"""Range-aware # noqa suppression (D3/D6).

A finding is suppressed if a ``# noqa`` (blanket or naming its code) appears on
any line within the blocker call site's range. The blocker line is the
authoritative anchor (invariant under resolver changes); the entry line is also
honored as best-effort.
"""

from __future__ import annotations

from .findings import Finding
from .ir import ModuleIR


def is_suppressed(finding: Finding, modules_by_path: dict) -> bool:
    code = finding.code.value
    # Blocker site (authoritative)
    mod = modules_by_path.get(finding.path)
    if mod is not None and _line_suppressed(mod, finding.lineno, code):
        return True
    # Entry site (best-effort)
    emod = modules_by_path.get(finding.entry_path)
    if emod is not None and _line_suppressed(emod, finding.entry_lineno, code):
        return True
    return False


def _line_suppressed(module: ModuleIR, lineno: int, code: str) -> bool:
    codes = module.noqa_lines.get(lineno)
    if not codes:
        return False
    if "" in codes:        # blanket noqa
        return True
    return code in codes
