"""Framework role disposition (D2 / ADR-0008).

Decides, by *structural shape* matched against the registry shape-DSL, whether a
function declared with a framework decorator runs OFF the event loop (FastAPI/
Starlette threadpool a plain ``def`` handler/dependency). No verb set lives in
this logic — the method/marker names are registry data.

Uniform optimism (ADR-0008): if a function carries a decorator whose shape
*might* be a framework registration but we cannot prove it, we treat a plain
``def`` as a (possibly threadpooled) handler -> off-loop, to avoid false
positives. Recall is recovered by the taint walk's dual-context direct-call
analysis (a plain ``def`` reached directly from an on-loop frame is still
analyzed on-loop).
"""

from __future__ import annotations

from .ir import FunctionIR
from .registry import Registry


def looks_like_framework_handler(func: FunctionIR, registry: Registry) -> bool:
    """True if ``func``'s decorators structurally match a framework registration
    shape (so a plain ``def`` body would be threadpooled / off-loop)."""
    for deco in func.decorators:
        for shape in registry.framework_shapes:
            if shape.kind == "decorator-attr-call" and deco.kind == "attr-call":
                if deco.attr in shape.attrs:
                    return True
            elif shape.kind == "decorator-bare-call" and deco.kind == "bare-call":
                if deco.attr in shape.attrs:
                    return True
    return False


def is_on_loop_root(func: FunctionIR, registry: Registry, framework_detect: bool) -> bool:
    """Whether ``func`` is itself an on-loop *root* entry point.

    Roots are async def functions (language fact). A plain ``def`` is never a
    root (it either runs in a threadpool when framework-registered, or is only
    reached via direct calls, handled by dual-context analysis).
    """
    if not func.is_async:
        return False
    # An async def is always on-loop when run by the framework or called directly.
    return True


def entry_disposition_off_loop(func: FunctionIR, registry: Registry,
                               framework_detect: bool) -> bool:
    """For the *entry overlay*: does framework registration place this plain
    ``def`` off the loop? Only meaningful for non-async functions.

    Returns True when we should NOT treat this def as an on-loop entry via its
    decorator (it's threadpooled, or its role is unresolved -> optimistic
    off-loop). Async defs are never off-loop entries.
    """
    if func.is_async:
        return False
    if not framework_detect:
        return False
    # Plain def with a framework-handler decorator -> threadpooled -> off-loop.
    # Plain def with an unresolved decorator shape -> optimistic off-loop too,
    # but that is the default (we simply don't add it as an entry), so the only
    # thing we assert here is the proven-threadpool case for documentation.
    return looks_like_framework_handler(func, registry)
