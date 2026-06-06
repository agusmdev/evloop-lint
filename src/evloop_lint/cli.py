"""Command-line interface (D7).

Exit codes:
  0  no findings at/above the confidence floor (or --exit-zero)
  1  findings at/above the floor that are CI-failing
  2  usage / config / internal error
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .codes import DEFAULT_FAILING_CODES, Code, Confidence
from .config import merge_config
from .engine import analyze_paths
from .report import render


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evloop-lint",
        description="Detect event-loop-blocking calls reachable from async code.",
    )
    p.add_argument("paths", nargs="*", default=["."], help="files or directories to analyze")
    p.add_argument("--version", action="version", version=f"evloop-lint {__version__}")
    p.add_argument("--max-depth", type=int, default=None, help="max call hops to follow (default 4)")
    p.add_argument("--confidence", choices=["definite", "probable", "possible"],
                   default=None, help="minimum confidence tier to report (default definite)")
    p.add_argument("--format", dest="fmt",
                   choices=["text", "json", "ndjson", "sarif", "github"],
                   default=None, help="output format")
    p.add_argument("--select", default=None,
                   help="comma-separated rule codes to include (default: all)")
    p.add_argument("--ignore", default=None,
                   help="comma-separated rule codes to exclude")
    p.add_argument("--exclude", default=None, help="comma-separated path globs to exclude")
    p.add_argument("--include", default=None, help="comma-separated path globs to force-include")
    p.add_argument("--no-chain", action="store_true", default=None,
                   help="omit the blocking chain trace in text output")
    p.add_argument("--no-fix-hints", action="store_true", default=None,
                   help="omit suggested async replacements")
    p.add_argument("--no-framework-detect", action="store_true", default=None,
                   help="treat every async def as on-loop, no framework overlay")
    p.add_argument("--strict", action="store_true", default=None,
                   help="parse errors cause a non-zero exit")
    p.add_argument("--exit-zero", action="store_true", default=None,
                   help="always exit 0 (report only)")
    p.add_argument("--statistics", action="store_true", default=None,
                   help="print coverage / truncation statistics")
    p.add_argument("--no-color", action="store_true", default=False,
                   help="disable ANSI colors")
    p.add_argument("--max-depth-budget", type=int, default=None, dest="tier3_budget",
                   help="tier-3 candidate fanout budget before EVL006 (default 8)")
    p.add_argument("--isolated", action="store_true", default=False,
                   help="ignore pyproject.toml config")
    return p


def _split_codes(val):
    if not val:
        return None
    return {c.strip().upper() for c in val.split(",") if c.strip()}


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    cli = {
        "max_depth": args.max_depth,
        "confidence": (Confidence(args.confidence) if args.confidence else None),
        "fmt": args.fmt,
        "select": _split_codes(args.select),
        "ignore": _split_codes(args.ignore),
        "exclude": (args.exclude.split(",") if args.exclude else None),
        "include": (args.include.split(",") if args.include else None),
        "no_chain": args.no_chain,
        "no_fix_hints": args.no_fix_hints,
        "no_framework_detect": args.no_framework_detect,
        "strict": args.strict,
        "exit_zero": args.exit_zero,
        "statistics": args.statistics,
        "color": (False if args.no_color else None),
        "tier3_budget": args.tier3_budget,
    }
    cli = {k: v for k, v in cli.items() if v is not None}

    try:
        cfg = merge_config(cli, list(args.paths) or ["."], isolated=args.isolated)
    except Exception as exc:  # config error
        print(f"evloop-lint: config error: {exc}", file=sys.stderr)
        return 2

    try:
        result = analyze_paths(cfg)
    except Exception as exc:  # internal error
        print(f"evloop-lint: internal error: {exc}", file=sys.stderr)
        return 2

    use_color = cfg.color and sys.stdout.isatty()
    if cfg.fmt == "text":
        use_color = cfg.color and sys.stdout.isatty()
    else:
        use_color = False

    output = render(
        result.findings, cfg.fmt,
        use_color=use_color,
        show_chain=not cfg.no_chain,
        show_fix=not cfg.no_fix_hints,
    )
    if output:
        print(output)

    # warnings (D7 orthogonality)
    for w in result.warnings:
        print(f"evloop-lint: warning: {w}", file=sys.stderr)

    if cfg.statistics:
        _print_statistics(result, cfg)

    # exit code
    if cfg.exit_zero:
        return 0
    if cfg.strict and result.parse_errors:
        return 1
    if result.has_failing(DEFAULT_FAILING_CODES):
        return 1
    # non-default-failing findings still present at/above floor -> also fail,
    # since the user explicitly raised the floor to see them.
    if result.findings and cfg.confidence != Confidence.DEFINITE:
        return 1
    return 0


def _print_statistics(result, cfg):
    out = sys.stderr
    print("", file=out)
    print("evloop-lint statistics:", file=out)
    print(f"  files analyzed: {result.files_analyzed}", file=out)
    print(f"  files skipped (parse errors): {result.files_skipped}", file=out)
    print(f"  findings reported: {len(result.findings)}", file=out)
    if result.truncation_count and cfg.confidence == Confidence.DEFINITE:
        print(f"  {result.truncation_count} potential chain(s) truncated at "
              f"--max-depth {cfg.max_depth}; run --confidence=possible or raise "
              f"--max-depth to see them", file=out)


if __name__ == "__main__":
    raise SystemExit(main())
