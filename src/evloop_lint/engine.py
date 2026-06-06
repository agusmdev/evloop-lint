"""Analysis engine: discover files -> IR -> index -> taint -> filter (D4/D5/D7)."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .codes import Code, Confidence, POSSIBLE_ONLY_CODES, confidence_at_or_above
from .config import Config
from .findings import Finding
from .registry import BlockerSpec, default_registry
from .resolver import ProjectIndex
from .suppress import is_suppressed
from .taint import TaintWalker
from .visitor import build_module_ir

_DEFAULT_EXCLUDES = [
    "*/.git/*", "*/.venv/*", "*/venv/*", "*/node_modules/*",
    "*/__pycache__/*", "*/.tox/*", "*/build/*", "*/dist/*",
    "*/.mypy_cache/*", "*/site-packages/*",
]


@dataclass
class AnalysisResult:
    findings: list = field(default_factory=list)       # filtered, suppression-applied
    all_findings: list = field(default_factory=list)   # before confidence/select filter
    parse_errors: list = field(default_factory=list)   # list[(path, msg)]
    files_analyzed: int = 0
    files_skipped: int = 0
    warnings: list = field(default_factory=list)
    truncation_count: int = 0

    def has_failing(self, failing_codes) -> bool:
        return any(f.code in failing_codes for f in self.findings)


def _module_name_for(path: str, roots: list) -> str:
    """Infer a dotted module name from a file path by walking up while
    ``__init__.py`` exists (package root detection)."""
    abspath = os.path.abspath(path)
    directory = os.path.dirname(abspath)
    base = os.path.splitext(os.path.basename(abspath))[0]
    parts = [base] if base != "__init__" else []
    cur = directory
    while os.path.isfile(os.path.join(cur, "__init__.py")):
        parts.append(os.path.basename(cur))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    parts.reverse()
    return ".".join(parts) if parts else base


def _iter_py_files(paths, excludes, includes):
    seen = set()
    all_excludes = _DEFAULT_EXCLUDES + list(excludes)
    for p in paths:
        if os.path.isfile(p):
            if p.endswith(".py"):
                rp = os.path.abspath(p)
                if rp not in seen:
                    seen.add(rp)
                    yield p
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            # prune excluded dirs early
            dirnames[:] = [
                d for d in dirnames
                if not _excluded(os.path.join(dirpath, d) + "/", all_excludes, includes)
            ]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                if _excluded(full, all_excludes, includes):
                    continue
                rp = os.path.abspath(full)
                if rp in seen:
                    continue
                seen.add(rp)
                yield full


def _excluded(path: str, excludes, includes) -> bool:
    norm = path.replace(os.sep, "/")
    for inc in includes:
        if fnmatch.fnmatch(norm, inc):
            return False
    for ex in excludes:
        if fnmatch.fnmatch(norm, ex):
            return True
    return False


def _apply_extensions(registry, cfg: Config):
    for dotted, msg in cfg.extend_blockers.items():
        registry.blockers[dotted] = BlockerSpec(
            dotted=dotted, code=Code.SYNC_IO, message=str(msg))


def analyze_paths(cfg: Config) -> AnalysisResult:
    registry = default_registry()
    _apply_extensions(registry, cfg)

    result = AnalysisResult()
    modules = []
    files = list(_iter_py_files(cfg.paths, cfg.exclude, cfg.include))

    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            result.parse_errors.append((path, str(exc)))
            result.files_skipped += 1
            continue
        module_name = _module_name_for(path, cfg.paths)
        mir = build_module_ir(source, module_name, path)
        if mir.parse_error:
            result.parse_errors.append((path, mir.parse_error))
            result.files_skipped += 1
            continue
        modules.append(mir)
        result.files_analyzed += 1

    index = ProjectIndex(modules, registry, tier3_budget=cfg.tier3_budget)
    walker = TaintWalker(index, max_depth=cfg.max_depth)
    raw = walker.run()
    result.all_findings = raw

    modules_by_path = {m.path: m for m in modules}

    # Filter: confidence floor, select/ignore, suppression.
    out = []
    for f in raw:
        if f.code == Code.DEPTH_TRUNCATION:
            result.truncation_count += 1
        if not confidence_at_or_above(f.confidence, cfg.confidence):
            continue
        if cfg.select and f.code.value not in cfg.select:
            continue
        if f.code.value in cfg.ignore:
            continue
        if is_suppressed(f, modules_by_path):
            continue
        out.append(f)

    # deterministic ordering
    out.sort(key=lambda f: (f.path, f.lineno, f.col, f.code.value))
    result.findings = out

    # select/confidence orthogonality warning (D7)
    for code_str in cfg.select:
        try:
            code = Code(code_str)
        except ValueError:
            continue
        if code in POSSIBLE_ONLY_CODES and cfg.confidence != Confidence.POSSIBLE:
            result.warnings.append(
                f"{code.value} only emits at --confidence=possible; "
                f"currently filtered by --confidence={cfg.confidence.value}"
            )
    return result
