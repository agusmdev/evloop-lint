"""evloop-lint: detect event-loop-blocking calls reachable from async code.

Public API is intentionally small; the CLI in :mod:`evloop_lint.cli` is the
primary entry point. The :func:`analyze_paths` helper is exposed for embedding
and for the adversarial test harness.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "analyze_paths", "AnalysisResult"]


def __getattr__(name):
    # Lazy re-export so importing submodules doesn't require the whole stack.
    if name in ("analyze_paths", "AnalysisResult"):
        from . import engine

        return getattr(engine, name)
    raise AttributeError(name)
