"""Test harness: analyze an in-memory multi-file project and return findings.

Each case is a dict of {module_path: source}. We write to a temp dir, run the
real engine, and return the AnalysisResult. This is also the seed benchmark
format (ADR-0004): a case + its expected findings.
"""

from __future__ import annotations

import os
import tempfile

from evloop_lint.config import Config
from evloop_lint.codes import Confidence
from evloop_lint.engine import analyze_paths


def run_project(files: dict, *, max_depth: int = 4,
                confidence: Confidence = Confidence.DEFINITE,
                select=None, ignore=None, tier3_budget: int = 8):
    """files: {"app/main.py": "<source>", ...}. Returns AnalysisResult."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, src in files.items():
            full = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(src)
        cfg = Config(
            paths=[tmp], max_depth=max_depth, confidence=confidence,
            select=set(select or []), ignore=set(ignore or []),
            tier3_budget=tier3_budget,
        )
        result = analyze_paths(cfg)
        # rewrite absolute paths back to relative for stable assertions
        for f in result.findings:
            f.path = os.path.relpath(f.path, tmp)
            f.entry_path = os.path.relpath(f.entry_path, tmp) if f.entry_path else f.entry_path
        return result


def codes(result):
    return sorted(f.code.value for f in result.findings)


def has(result, code, *, line=None, path=None):
    for f in result.findings:
        if f.code.value != code:
            continue
        if line is not None and f.lineno != line:
            continue
        if path is not None and f.path != path:
            continue
        return True
    return False


def find_all(result, code):
    return [f for f in result.findings if f.code.value == code]
