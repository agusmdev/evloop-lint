"""Configuration (D7): pyproject.toml [tool.evloop-lint] + CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

from .codes import Confidence


@dataclass
class Config:
    paths: list = field(default_factory=lambda: ["."])
    max_depth: int = 4
    confidence: Confidence = Confidence.DEFINITE
    fmt: str = "text"
    select: set = field(default_factory=set)        # rule codes to include (empty = all)
    ignore: set = field(default_factory=set)        # rule codes to exclude
    exclude: list = field(default_factory=list)     # glob path excludes
    include: list = field(default_factory=list)     # glob path force-includes
    no_chain: bool = False
    no_fix_hints: bool = False
    no_framework_detect: bool = False
    strict: bool = False                            # parse errors fail the build
    exit_zero: bool = False
    statistics: bool = False
    color: bool = True
    jobs: int = 0                                   # 0 = auto
    baseline: str = ""
    tier3_budget: int = 8

    # extension registries (config DATA, not logic)
    extend_blockers: dict = field(default_factory=dict)


def _parse_confidence(val: str) -> Confidence:
    val = (val or "definite").lower()
    return {"definite": Confidence.DEFINITE,
            "probable": Confidence.PROBABLE,
            "possible": Confidence.POSSIBLE}.get(val, Confidence.DEFINITE)


def load_pyproject(start: str) -> dict:
    """Find the nearest pyproject.toml walking up from ``start`` and return its
    [tool.evloop-lint] table (empty dict if none)."""
    if tomllib is None:
        return {}
    cur = os.path.abspath(start)
    if os.path.isfile(cur):
        cur = os.path.dirname(cur)
    while True:
        candidate = os.path.join(cur, "pyproject.toml")
        if os.path.isfile(candidate):
            try:
                with open(candidate, "rb") as fh:
                    data = tomllib.load(fh)
                return data.get("tool", {}).get("evloop-lint", {}) or {}
            except (OSError, ValueError):
                return {}
        parent = os.path.dirname(cur)
        if parent == cur:
            return {}
        cur = parent


def merge_config(cli: dict, paths: list, isolated: bool = False) -> Config:
    """Merge: CLI > pyproject > defaults (D7)."""
    cfg = Config()
    if not isolated:
        anchor = paths[0] if paths else "."
        table = load_pyproject(anchor)
        if "max-depth" in table:
            cfg.max_depth = int(table["max-depth"])
        if "confidence" in table:
            cfg.confidence = _parse_confidence(table["confidence"])
        if "format" in table:
            cfg.fmt = str(table["format"])
        if "select" in table:
            cfg.select = {str(c).upper() for c in table["select"]}
        if "ignore" in table:
            cfg.ignore = {str(c).upper() for c in table["ignore"]}
        if "exclude" in table:
            cfg.exclude = list(table["exclude"])
        if "include" in table:
            cfg.include = list(table["include"])
        if "no-framework-detect" in table:
            cfg.no_framework_detect = bool(table["no-framework-detect"])
        if "tier3-budget" in table:
            cfg.tier3_budget = int(table["tier3-budget"])
        if "extend-blockers" in table and isinstance(table["extend-blockers"], dict):
            cfg.extend_blockers = dict(table["extend-blockers"])

    # CLI overrides
    for k, v in cli.items():
        if v is None:
            continue
        setattr(cfg, k, v)
    cfg.paths = paths or cfg.paths
    return cfg
