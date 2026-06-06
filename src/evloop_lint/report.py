"""Output formatters (D6): text (with chain), json/ndjson, sarif, github."""

from __future__ import annotations

import json

from .findings import Finding

_RESET = "\033[0m"
_BOLD = "\033[1m"
_RED = "\033[31m"
_YEL = "\033[33m"
_DIM = "\033[2m"
_CYAN = "\033[36m"


def _c(text: str, color: str, use_color: bool) -> str:
    return f"{color}{text}{_RESET}" if use_color else text


def format_text(findings: list, *, show_chain: bool = True, use_color: bool = True,
                show_fix: bool = True) -> str:
    lines = []
    for f in findings:
        tier = f.confidence.value
        head = (
            f"{_c(f.location(), _BOLD, use_color)} "
            f"{_c(f.code.value, _RED if tier == 'definite' else _YEL, use_color)} "
            f"[{tier}] {f.message}"
        )
        lines.append(head)
        if show_chain and f.chain:
            for i, step in enumerate(f.chain):
                arrow = "  " if i == 0 else "  -> "
                lbl = f" ({step.label})" if step.label else ""
                lines.append(
                    _c(f"{arrow}{step.qualname} "
                       f"[{step.path}:{step.lineno}]{lbl}", _DIM, use_color)
                )
        if show_fix and f.suggested_fix:
            lines.append(_c(f"  fix: consider {f.suggested_fix}", _CYAN, use_color))
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def _finding_dict(f: Finding) -> dict:
    return {
        "code": f.code.value,
        "confidence": f.confidence.value,
        "path": f.path,
        "line": f.lineno,
        "column": f.col + 1,
        "message": f.message,
        "entry": {"qualname": f.entry_qualname, "path": f.entry_path, "line": f.entry_lineno},
        "blocker": f.blocker_qualname,
        "depth": f.depth,
        "fingerprint": f.fingerprint,
        "suggested_fix": f.suggested_fix,
        "chain": [
            {"qualname": s.qualname, "path": s.path, "line": s.lineno, "label": s.label}
            for s in f.chain
        ],
    }


def format_json(findings: list) -> str:
    return json.dumps([_finding_dict(f) for f in findings], indent=2)


def format_ndjson(findings: list) -> str:
    return "\n".join(json.dumps(_finding_dict(f)) for f in findings)


def format_github(findings: list) -> str:
    out = []
    for f in findings:
        level = "error" if f.confidence.value == "definite" else "warning"
        msg = f"{f.code.value} {f.message}"
        out.append(f"::{level} file={f.path},line={f.lineno},col={f.col + 1}::{msg}")
    return "\n".join(out)


def format_sarif(findings: list, *, max_chain: int = 32) -> str:
    rules = {}
    results = []
    for f in findings:
        rid = f.code.value
        if rid not in rules:
            rules[rid] = {
                "id": rid,
                "name": f.code.name,
                "shortDescription": {"text": f.code.title},
            }
        locations = [{
            "physicalLocation": {
                "artifactLocation": {"uri": f.path},
                "region": {"startLine": f.lineno, "startColumn": f.col + 1},
            }
        }]
        thread_flow_locations = []
        for s in f.chain[:max_chain]:
            thread_flow_locations.append({
                "location": {
                    "physicalLocation": {
                        "artifactLocation": {"uri": s.path},
                        "region": {"startLine": s.lineno},
                    },
                    "message": {"text": s.label or s.qualname},
                }
            })
        results.append({
            "ruleId": rid,
            "level": "error" if f.confidence.value == "definite" else "warning",
            "message": {"text": f"{f.message} ({f.confidence.value})"},
            "locations": locations,
            "partialFingerprints": {"evloopFingerprint": f.fingerprint},
            "codeFlows": [{
                "threadFlows": [{"locations": thread_flow_locations}]
            }] if thread_flow_locations else [],
        })
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "evloop-lint",
                "rules": list(rules.values()),
            }},
            "results": results,
        }],
    }
    return json.dumps(sarif, indent=2)


def render(findings: list, fmt: str, *, use_color: bool = True,
           show_chain: bool = True, show_fix: bool = True) -> str:
    if fmt == "text":
        return format_text(findings, show_chain=show_chain, use_color=use_color, show_fix=show_fix)
    if fmt == "json":
        return format_json(findings)
    if fmt == "ndjson":
        return format_ndjson(findings)
    if fmt == "github":
        return format_github(findings)
    if fmt == "sarif":
        return format_sarif(findings)
    raise ValueError(f"unknown format: {fmt}")
