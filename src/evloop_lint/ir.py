"""Intermediate representation (D3).

A single ``ast`` pass per file produces a compact, picklable IR; the AST is then
discarded. The IR stores *raw structural facts only* — it makes zero framework /
verb / blocker judgments. All role decisions are deferred to the registry at link
time (keeps specific identifiers out of the visitor → genericity).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class CallableRef:
    """A reference to a callable appearing as an argument (for offload/schedule).

    Exactly one of the fields below is meaningful, in priority order:
      - ``name_chain``: a dotted name expression, e.g. ``helper`` or ``mod.fn``
        or ``self.compute`` (stored as the attribute chain).
      - ``lambda_calls``: call sites inside an inline ``lambda`` body.
      - ``is_unresolvable``: a dynamic expression (subscript, call result, etc.).
    """

    name_chain: tuple = ()             # e.g. ("self", "repo", "find")
    is_lambda: bool = False
    lambda_calls: list = field(default_factory=list)   # list[CallIR]
    is_unresolvable: bool = False
    # If this ref is itself a call (e.g. functools.partial(fn, ...)), record the
    # wrapper's func chain and its positional argument refs so the resolver can
    # consult the wrapper registry and unwrap to the real target callable.
    wrapper_chain: tuple = ()          # e.g. ("functools", "partial")
    wrapper_arg_refs: list = field(default_factory=list)  # list[CallableRef]


@dataclass
class CallIR:
    """A call site inside a function body."""

    # Dotted attribute/name chain of the *callee*, e.g. ("requests", "get") or
    # ("helper",) or ("self", "repo", "find").
    func_chain: tuple
    lineno: int
    col: int
    end_lineno: int
    is_awaited: bool = False           # appears as `await <call>`
    is_bare_expr: bool = False         # statement-level call whose value is discarded
    # For offload/schedule resolution: the callable-bearing arguments by position.
    arg_refs: list = field(default_factory=list)        # list[CallableRef] (positional)
    keyword_arg_names: tuple = ()       # names of keyword args present
    # Nested call expressions appearing inside this call's argument list (these
    # are evaluated eagerly on-loop — eager-evaluation rule, D1).
    eager_arg_calls: list = field(default_factory=list)  # list[CallIR]
    # If the callee is itself a call result chain (e.g. factory().method()), we
    # mark it so the resolver can treat it as Tier-3/unresolvable appropriately.
    callee_is_call_result: bool = False


@dataclass
class AttrAccessIR:
    """A bare attribute LOAD (e.g. ``settings.dsn``) — a potential @property
    getter invocation (implicit call channel, D8)."""

    attr: str                           # the accessed attribute name
    receiver_chain: tuple               # dotted chain of the receiver
    lineno: int
    col: int
    end_lineno: int


@dataclass
class LoopIR:
    """A loop construct, for EVL003 structural-unbounded detection (D8)."""

    lineno: int
    col: int
    end_lineno: int
    is_unbounded: bool                  # `while True`/`while 1` or for-over-unknown
    has_yielding_await: bool            # contains await to a (candidate) yielding prim
    await_targets: tuple = ()           # dotted chains of awaited calls inside
    body_calls: tuple = ()              # dotted chains of plain calls inside (for CPU re-raise)


@dataclass
class FunctionIR:
    qualname: str                       # module-qualified, e.g. "app.svc.UserService.find"
    name: str                           # bare name
    is_async: bool
    lineno: int
    col: int
    end_lineno: int
    decorators: list = field(default_factory=list)   # list[DecoratorShape]
    calls: list = field(default_factory=list)        # list[CallIR]
    attr_loads: list = field(default_factory=list)   # list[AttrAccessIR]
    loops: list = field(default_factory=list)        # list[LoopIR]
    enclosing_class: str = ""           # bare class name if a method, else ""
    is_method: bool = False
    param_names: tuple = ()             # parameter names (for self/param detection)


@dataclass
class DecoratorShape:
    """Raw structural facts about a decorator (D3). No verb judgments here."""

    kind: str                           # "attr-call" | "bare-call" | "attr" | "name" | "other"
    receiver_chain: tuple = ()          # for attr forms: the receiver dotted chain
    attr: str = ""                      # the trailing attribute / name
    is_call: bool = False
    lineno: int = 0


@dataclass
class ImportIR:
    """A resolved import binding: local name -> dotted module/symbol target."""

    local_name: str
    dotted_target: str                  # e.g. "time" or "time.sleep" or "app.db.query"
    is_module: bool                     # True for `import x`; False for `from x import y`


@dataclass
class ClassIR:
    name: str
    qualname: str
    bases: tuple = ()                   # base dotted chains
    method_names: tuple = ()


@dataclass
class ModuleIR:
    module: str                         # dotted module name, e.g. "app.services.user"
    path: str
    imports: list = field(default_factory=list)       # list[ImportIR]
    functions: list = field(default_factory=list)     # list[FunctionIR] (all nesting levels)
    classes: list = field(default_factory=list)       # list[ClassIR]
    noqa_lines: dict = field(default_factory=dict)     # lineno -> set[str] codes ("" = blanket)
    parse_error: str = ""               # non-empty if the file failed to parse
