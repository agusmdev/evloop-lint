"""Project index + call resolution (ADR-0002).

The resolver maps a call site to one of:
  - a registry edge (blocker / offload / scheduler), or
  - a project ``FunctionIR`` (Tier 1-2: import/alias/dotted name resolution), or
  - Tier-3 heuristic method candidates (instance.method() by method name), or
  - nothing (unresolved -> optimistic-safe, ADR-0003).

It is the IP the adversarial loop iterates on, so it owns all of this rather than
delegating to astroid/PyCG. Logic here is name-agnostic; the only specific
identifiers come from the registry data it consults.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import CallableRef, CallIR, FunctionIR, ModuleIR
from .registry import BlockerSpec, OffloadSpec, Registry, ScheduleOnLoopSpec


def _is_property(func) -> bool:
    """A method decorated with a bare ``@property`` (or ``@x.setter``-less
    cached-property-like) getter. Generic: matches the structural decorator name
    'property' / a '*property*' bare/attr decorator, which is the language
    descriptor protocol, not an app-specific identifier."""
    for d in func.decorators:
        if d.kind in ("name", "attr") and "property" in d.attr.lower():
            return True
        if d.kind in ("bare-call", "attr-call") and "property" in d.attr.lower():
            return True
    return False


# Kinds of resolution outcome.
KIND_BLOCKER = "blocker"
KIND_OFFLOAD = "offload"
KIND_SCHEDULER = "scheduler"
KIND_FUNCTION = "function"
KIND_TIER3 = "tier3"
KIND_UNRESOLVED = "unresolved"


@dataclass
class Resolution:
    kind: str
    blocker: BlockerSpec | None = None
    offload: OffloadSpec | None = None
    scheduler: ScheduleOnLoopSpec | None = None
    function: FunctionIR | None = None          # exact project function (Tier 1-2)
    tier3_candidates: tuple = ()                  # tuple[FunctionIR] (heuristic)
    tier3_overflow: bool = False                 # too many candidates (EVL006)
    dotted_target: str = ""                      # best-effort dotted form (for messages)


class ProjectIndex:
    """Indexes all modules and answers resolution queries."""

    def __init__(self, modules: list, registry: Registry, tier3_budget: int = 8):
        self.registry = registry
        self.tier3_budget = tier3_budget
        self.modules: dict = {}                  # module name -> ModuleIR
        self.functions_by_qual: dict = {}        # qualname -> FunctionIR
        self.methods_by_name: dict = {}          # method bare name -> list[FunctionIR]
        self.functions_by_module_name: dict = {} # (module, bare_name) -> FunctionIR
        self.classes_by_name: dict = {}          # bare class name -> list[ClassIR]
        self.qual_by_suffix: dict = {}           # "service.fetch" -> [FunctionIR]
        self.methods_qual_by_suffix: dict = {}   # "Repo.find" -> [FunctionIR] (methods)
        self.class_by_qual: dict = {}            # class qualname -> ClassIR
        self.class_by_suffix: dict = {}          # "app.Repo" suffix -> [ClassIR]
        self.init_by_class_qual: dict = {}       # class qualname -> __init__ FunctionIR
        self.dunder_by_class_qual: dict = {}     # (class qualname, dunder) -> FunctionIR
        self.properties_by_name: dict = {}       # property getter name -> [FunctionIR]
        # Resolution memo. A CallIR is owned by exactly one function/module, so it
        # always resolves in the same (module, func) context regardless of how many
        # times the taint walk re-visits its owner (dual-context, multiple paths).
        # Keying on id(call) is therefore sound and collapses ~1M resolve_call hits.
        self._resolve_memo: dict = {}            # id(CallIR) -> Resolution
        for m in modules:
            self._index_module(m)

    def _index_module(self, m: ModuleIR):
        self.modules[m.module] = m
        for f in m.functions:
            self.functions_by_qual[f.qualname] = f
            # top-level (module.func) lookup table
            if not f.is_method:
                self.functions_by_module_name[(m.module, f.name)] = f
                # suffix index for cross-root matching (module-root ambiguity):
                # "service.fetch" suffix of qualname matches import "app.service.fetch".
                parts = f.qualname.split(".")
                for cut in range(len(parts)):
                    suffix = ".".join(parts[cut:])
                    self.qual_by_suffix.setdefault(suffix, []).append(f)
            else:
                self.methods_by_name.setdefault(f.name, []).append(f)
                # method suffix index (e.g. "Repo.find") for callable-ref Tier-3
                mparts = f.qualname.split(".")
                for cut in range(len(mparts)):
                    suffix = ".".join(mparts[cut:])
                    self.methods_qual_by_suffix.setdefault(suffix, []).append(f)
                # class-scoped dunder / __init__ / property indexes
                class_qual = f.qualname.rsplit("." + f.name, 1)[0]
                if f.name == "__init__":
                    self.init_by_class_qual[class_qual] = f
                if f.name.startswith("__") and f.name.endswith("__"):
                    self.dunder_by_class_qual[(class_qual, f.name)] = f
                if _is_property(f):
                    self.properties_by_name.setdefault(f.name, []).append(f)
        for c in m.classes:
            self.classes_by_name.setdefault(c.name, []).append(c)
            self.class_by_qual[c.qualname] = c
            cparts = c.qualname.split(".")
            for cut in range(len(cparts)):
                suffix = ".".join(cparts[cut:])
                self.class_by_suffix.setdefault(suffix, []).append(c)

    # ---- import/alias resolution -------------------------------------------
    def _expand_chain(self, module: ModuleIR, chain: tuple) -> str:
        """Expand a dotted name chain through module imports to a dotted target.

        e.g. with `import requests as rq`, chain ("rq","get") -> "requests.get".
        With `from app.db import query`, chain ("query",) -> "app.db.query".
        With `import app.db as db`, chain ("db","query") -> "app.db.query".
        Unknown leading names are left as-is (best-effort).
        """
        if not chain:
            return ""
        head = chain[0]
        rest = chain[1:]
        for imp in module.imports:
            if imp.local_name == head:
                base = imp.dotted_target
                if rest:
                    return base + "." + ".".join(rest)
                return base
        return ".".join(chain)

    # ---- public resolution --------------------------------------------------
    def resolve_call(self, call: CallIR, in_module: ModuleIR, in_func: FunctionIR) -> Resolution:
        cached = self._resolve_memo.get(id(call))
        if cached is not None:
            return cached
        res = self._resolve_call_uncached(call, in_module, in_func)
        self._resolve_memo[id(call)] = res
        return res

    def _resolve_call_uncached(self, call: CallIR, in_module: ModuleIR, in_func: FunctionIR) -> Resolution:
        chain = call.func_chain

        # Callee is a call result (factory().method()) or otherwise dynamic.
        if not chain:
            if call.callee_is_call_result:
                return Resolution(kind=KIND_UNRESOLVED, dotted_target="")
            return Resolution(kind=KIND_UNRESOLVED)

        dotted = self._expand_chain(in_module, chain)

        # 1) registry edges take priority (data) ------------------------------
        sched = self._match_scheduler(call, chain, dotted)
        if sched is not None:
            return sched
        off = self.registry.match_offload(dotted)
        if off is None and len(chain) >= 2:
            # also try the trailing 1-2 segments for method-style offloads
            off = self.registry.match_offload(".".join(chain[-2:]))
        if off is not None:
            return Resolution(kind=KIND_OFFLOAD, offload=off, dotted_target=dotted)
        blk = self.registry.match_blocker(dotted)
        if blk is None:
            blk = self.registry.match_blocker(".".join(chain))
        if blk is not None:
            return Resolution(kind=KIND_BLOCKER, blocker=blk, dotted_target=dotted)

        # 2) project function — Tier 1-2 (top-level / imported) ---------------
        fn = self._resolve_project_function(in_module, chain, dotted)
        if fn is not None:
            return Resolution(kind=KIND_FUNCTION, function=fn, dotted_target=fn.qualname)

        # 2b) class constructor — ClassName(...) -> its __init__ body ---------
        init_fn = self._resolve_constructor(in_module, chain, dotted)
        if init_fn is not None:
            return Resolution(kind=KIND_FUNCTION, function=init_fn,
                              dotted_target=init_fn.qualname)

        # 3) Tier-3 heuristic method match (instance.method()) ----------------
        if len(chain) >= 2:
            method = chain[-1]
            cands = self.methods_by_name.get(method, [])
            if cands:
                if len(cands) > self.tier3_budget:
                    return Resolution(kind=KIND_TIER3, tier3_candidates=tuple(cands),
                                      tier3_overflow=True, dotted_target=method)
                return Resolution(kind=KIND_TIER3, tier3_candidates=tuple(cands),
                                  dotted_target=method)

        return Resolution(kind=KIND_UNRESOLVED, dotted_target=dotted)

    def _match_scheduler(self, call: CallIR, chain: tuple, dotted: str) -> Resolution | None:
        spec = self.registry.match_scheduler(dotted)
        if spec is None and chain:
            spec = self.registry.match_scheduler(".".join(chain))
        if spec is None and chain:
            spec = self.registry.match_scheduler(chain[-1])
        if spec is not None:
            return Resolution(kind=KIND_SCHEDULER, scheduler=spec, dotted_target=dotted)
        return None

    def _resolve_project_function(self, module: ModuleIR, chain: tuple, dotted: str):
        # direct qualname hit (dotted target is a known function)
        if dotted in self.functions_by_qual:
            return self.functions_by_qual[dotted]
        # bare local name -> same-module top-level function
        if len(chain) == 1:
            f = self.functions_by_module_name.get((module.module, chain[0]))
            if f is not None:
                return f
            # maybe imported: from x import f
            if dotted in self.functions_by_qual:
                return self.functions_by_qual[dotted]
            return self._suffix_resolve(dotted, chain_len=len(chain))
        # dotted module.func -> look up by (module, name)
        target_mod = ".".join(dotted.split(".")[:-1])
        target_name = dotted.split(".")[-1]
        f = self.functions_by_module_name.get((target_mod, target_name))
        if f is not None:
            return f
        return self._suffix_resolve(dotted, chain_len=len(chain))

    def _suffix_resolve(self, dotted: str, chain_len: int = 1):
        """Cross-root fallback: match an import target like ``app.service.fetch``
        to a function indexed as ``service.fetch`` (root-prefix ambiguity when the
        analyzed tree is not itself the import root). Pick the LONGEST suffix that
        resolves to exactly one function (unambiguous).

        A module-level function is only ever reached cross-root through a suffix
        that still names its containing module (e.g. ``service.fetch``), never
        through a *bare* function name. So a bare 1-segment suffix may satisfy only
        a 1-segment call (an imported/global name like ``fetch()``). When the call
        site is a multi-segment ATTRIBUTE access (``obj.attr.method()``), matching
        the bare trailing name to a same-named module-level ``def`` is a name
        collision, not a real edge — those must fall through to Tier-3 method
        matching against actual methods instead. (Guards EVL004/EVL001 FPs from a
        sync method colliding with a module-level coroutine of the same name.)"""
        if not dotted:
            return None
        parts = dotted.split(".")
        # Bare trailing name is only a valid module-function suffix for a bare call.
        min_suffix_len = 1 if chain_len <= 1 else 2
        for cut in range(len(parts)):
            suffix_parts = parts[cut:]
            if len(suffix_parts) < min_suffix_len:
                continue
            suffix = ".".join(suffix_parts)
            if suffix == dotted:
                continue  # already tried as full dotted
            cands = self.qual_by_suffix.get(suffix)
            if cands and len(cands) == 1:
                return cands[0]
        return None

    def _resolve_class(self, module: ModuleIR, chain: tuple, dotted: str):
        """Resolve a name/dotted chain to a project ClassIR, if unambiguous."""
        if dotted in self.class_by_qual:
            return self.class_by_qual[dotted]
        # bare name in same module
        if len(chain) == 1:
            cands = self.classes_by_name.get(chain[0])
            if cands and len(cands) == 1:
                return cands[0]
        # suffix match (cross-root)
        parts = dotted.split(".")
        for cut in range(len(parts)):
            suffix = ".".join(parts[cut:])
            cands = self.class_by_suffix.get(suffix)
            if cands and len(cands) == 1:
                return cands[0]
        # last segment bare-name fallback
        cands = self.classes_by_name.get(chain[-1]) if chain else None
        if cands and len(cands) == 1:
            return cands[0]
        return None

    def _resolve_constructor(self, module: ModuleIR, chain: tuple, dotted: str):
        """If a call target names a project class, return its __init__ body."""
        cls = self._resolve_class(module, chain, dotted)
        if cls is None:
            return None
        return self.init_by_class_qual.get(cls.qualname)

    def lookup_dunder(self, cls_qual: str, dunder: str):
        return self.dunder_by_class_qual.get((cls_qual, dunder))

    # ---- resolving a callable reference (offload/schedule work arg) ----------
    def resolve_callable_ref(self, ref: CallableRef, in_module: ModuleIR,
                             in_func: FunctionIR):
        """Resolve the work-bearing callable of an offload/scheduler.

        Returns a list of (FunctionIR, confidence_is_probable) candidates. A
        bound-method or free-function reference resolves to one or more project
        functions; an unresolved/lambda ref returns []. Used for BOTH offload
        (off-loop) and scheduler (on-loop) work; the caller decides the context.
        """
        if ref.is_lambda:
            return []
        # Wrapper unwrap (e.g. functools.partial(fn, ...)): recurse on the target
        # arg. Bound args of the wrapper are handled as eager on-loop calls elsewhere.
        if ref.is_unresolvable and ref.wrapper_chain:
            wdotted = self._expand_chain(in_module, ref.wrapper_chain)
            wspec = self.registry.match_wrapper(wdotted)
            if wspec is None:
                wspec = self.registry.match_wrapper(".".join(ref.wrapper_chain))
            if wspec is not None and 0 <= wspec.target_arg_index < len(ref.wrapper_arg_refs):
                return self.resolve_callable_ref(
                    ref.wrapper_arg_refs[wspec.target_arg_index], in_module, in_func)
            return []
        if ref.is_unresolvable:
            return []
        chain = ref.name_chain
        if not chain:
            return []
        dotted = self._expand_chain(in_module, chain)
        # Tier 1-2: exact / imported / suffix
        fn = self._resolve_project_function(in_module, chain, dotted)
        if fn is not None:
            return [(fn, False)]
        # constructor reference (rare for callable args, but be consistent)
        init_fn = self._resolve_constructor(in_module, chain, dotted)
        if init_fn is not None:
            return [(init_fn, False)]
        # Tier-3: bound-method reference like self._cache.reload -> method body.
        # Prefer a method-qualname suffix match (e.g. "CacheManager.reload");
        # fall back to bare method name across all classes.
        if len(chain) >= 2:
            method = chain[-1]
            # try 2-segment suffix "Class.method" if the receiver is a class name
            two = ".".join(chain[-2:])
            cands = self.methods_qual_by_suffix.get(two)
            if not cands:
                cands = self.methods_by_name.get(method, [])
            if cands and len(cands) <= self.tier3_budget:
                return [(c, True) for c in cands]
        return []
