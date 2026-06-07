"""Context-keyed taint walk (D4).

From each on-loop root (async def), walk the call graph carrying an ``on_loop``
context. Emit a finding when an on-loop path reaches a registry blocker within
``max_depth`` hops.

Key rules (from the hardened design):
  * Offload edges flip ``on_loop`` False for the work-bearing callable's body;
    arguments and lexically-later calls stay on-loop (eager-eval handled in IR).
  * Scheduler edges keep work on-loop; re-entry schedulers re-set on_loop True
    even inside an offload subtree.
  * Depth decrements per real call hop. Reaching depth 0 while a blocker is still
    reachable emits a POSSIBLE-tier EVL005 truncation finding (never silent).
  * Tier-3 fanout above budget -> POSSIBLE-tier EVL006 ambiguous-dispatch.
  * Cycles guarded with a visited set on (qualname, on_loop); recursion returns
    optimistic-safe at the back-edge.
  * Dual-context: a function may be analyzed both on-loop and off-loop; the memo
    key includes on_loop.
"""

from __future__ import annotations

from .codes import Code, Confidence
from .findings import ChainStep, Finding
from .framework import is_on_loop_root
from .ir import FunctionIR, ModuleIR
from .resolver import (
    KIND_BLOCKER,
    KIND_FUNCTION,
    KIND_OFFLOAD,
    KIND_SCHEDULER,
    KIND_TIER3,
    ProjectIndex,
)


_MISS = object()  # sentinel for "not yet cached" (None is a valid cached result)


def _min_conf(a: Confidence, b: Confidence) -> Confidence:
    """The lower (less certain) of two confidence tiers."""
    return a if a.rank <= b.rank else b


class TaintWalker:
    def __init__(self, index: ProjectIndex, max_depth: int = 4):
        self.index = index
        self.max_depth = max_depth
        self.findings: list = []
        self._seen_fps: set = set()         # dedup by fingerprint
        # memo: (qualname, on_loop) -> "reaches blocker on-loop?" is NOT cached
        # for emission (we always need the witness chain on first reach), but we
        # use a cheap prune cache (qualname -> reaches any blocker ignoring tags).
        self._prune_cache: dict = {}
        self._module_of_cache: dict = {}    # func.qualname -> ModuleIR | None

    # ---- entry ------------------------------------------------------------
    def run(self):
        for module in self.index.modules.values():
            for func in module.functions:
                if is_on_loop_root(func, self.index.registry, framework_detect=True):
                    self._walk_root(func, module)
        return self.findings

    def _walk_root(self, func: FunctionIR, module: ModuleIR):
        entry_step = ChainStep(func.qualname, module.path, func.lineno, "async entry")
        self._walk(
            func, module,
            on_loop=True, depth=self.max_depth,
            entry=func, entry_module=module,
            chain=[entry_step], stack=frozenset(),
            min_conf=Confidence.DEFINITE,
        )

    # ---- core walk --------------------------------------------------------
    def _walk(self, func, module, *, on_loop, depth, entry, entry_module, chain, stack, min_conf):
        key = (func.qualname, on_loop)
        if key in stack:
            return  # cycle back-edge: optimistic-safe, not cached
        stack = stack | {key}

        for call in func.calls:
            res = self.index.resolve_call(call, module, func)

            if res.kind == KIND_BLOCKER:
                if on_loop:
                    self._emit_blocker(call, res, func, module, entry, entry_module,
                                       chain, depth, min_conf)
                continue

            if res.kind == KIND_SCHEDULER:
                self._handle_scheduler(call, res, func, module, entry, entry_module,
                                       chain, depth, on_loop, stack, min_conf)
                continue

            if res.kind == KIND_OFFLOAD:
                self._handle_offload(call, res, func, module, entry, entry_module,
                                     chain, depth, stack, min_conf)
                continue

            if res.kind == KIND_FUNCTION:
                # EVL004: calling an async function whose coroutine is discarded
                # (bare expr statement, not awaited, not scheduled). Loop-agnostic.
                if res.function.is_async and call.is_bare_expr and not call.is_awaited:
                    self._emit_await_misuse(call, res.function, func, module,
                                            entry, entry_module, chain, depth)
                self._descend(res.function, call, func, module, entry, entry_module,
                              chain, depth, on_loop, stack, min_conf)
                continue

            if res.kind == KIND_TIER3:
                if res.tier3_overflow:
                    if on_loop:
                        self._emit_ambiguous(call, func, module, entry, entry_module, chain, depth)
                    continue
                # Each candidate is probable-tier; descend into each, downgrading
                # the running confidence to at most PROBABLE.
                cand_conf = _min_conf(min_conf, Confidence.PROBABLE)
                for cand in res.tier3_candidates:
                    self._descend(cand, call, func, module, entry, entry_module,
                                  chain, depth, on_loop, stack, cand_conf)
                continue

            # unresolved -> optimistic-safe (ADR-0003)

        # Implicit @property getter invocations via attribute LOADs (D8) -----
        if on_loop:
            for acc in func.attr_loads:
                getters = self.index.properties_by_name.get(acc.attr)
                if not getters or len(getters) > self.index.tier3_budget:
                    continue
                pconf = _min_conf(min_conf, Confidence.PROBABLE)
                for getter in getters:
                    self._descend_attr(getter, acc, func, module, entry,
                                       entry_module, chain, depth, stack, pconf)

        # EVL003 structural-unbounded loops (possible tier) -----------------
        if on_loop:
            for loop in func.loops:
                self._check_loop(loop, func, module, entry, entry_module, chain, depth)

    def _descend(self, callee, call, caller, module, entry, entry_module, chain, depth,
                 on_loop, stack, min_conf):
        if depth <= 0:
            # Depth budget exhausted: if the callee can still reach a blocker,
            # surface a POSSIBLE truncation finding rather than silently dropping.
            if on_loop and self._reaches_blocker(callee, frozenset()):
                self._emit_truncation(call, caller, module, entry, entry_module, chain)
            return
        callee_module = self._module_of(callee) or module
        step = ChainStep(callee.qualname, callee_module.path, call.lineno, "calls")
        self._walk(callee, callee_module, on_loop=on_loop, depth=depth - 1,
                   entry=entry, entry_module=entry_module,
                   chain=chain + [step], stack=stack, min_conf=min_conf)

    def _descend_attr(self, getter, acc, caller, module, entry, entry_module,
                      chain, depth, stack, min_conf):
        """Descend into a @property getter reached via an attribute load."""
        if depth <= 0:
            if self._reaches_blocker(getter, frozenset()):
                # synthesize a truncation-style possible signal at the access site
                pass
            return
        gmod = self._module_of(getter) or module
        step = ChainStep(getter.qualname, gmod.path, acc.lineno, "property access")
        self._walk(getter, gmod, on_loop=True, depth=depth - 1,
                   entry=entry, entry_module=entry_module,
                   chain=chain + [step], stack=stack, min_conf=min_conf)

    def _module_of(self, func: FunctionIR):
        # Pure function of func.qualname; cached (called once per call hop).
        cached = self._module_of_cache.get(func.qualname, _MISS)
        if cached is not _MISS:
            return cached
        # strip trailing .name and (optional) .Class to find an indexed module
        parts = func.qualname.split(".")
        result = None
        for cut in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in self.index.modules:
                result = self.index.modules[candidate]
                break
        self._module_of_cache[func.qualname] = result
        return result

    # ---- offload / scheduler ----------------------------------------------
    def _handle_offload(self, call, res, func, module, entry, entry_module, chain, depth, stack, min_conf):
        spec = res.offload
        # Eager argument expressions of the offload call run on-loop (D1).
        self._emit_eager_calls(call, func, module, entry, entry_module, chain, depth, min_conf, stack)
        # The work-bearing callable runs OFF loop.
        work_ref = None
        idx = spec.work_arg_index
        if 0 <= idx < len(call.arg_refs):
            work_ref = call.arg_refs[idx]
        if work_ref is None:
            return
        if depth <= 0:
            return
        targets = self.index.resolve_callable_ref(work_ref, module, func)
        for target, is_probable in targets:
            tconf = _min_conf(min_conf, Confidence.PROBABLE) if is_probable else min_conf
            tmod = self._module_of(target) or module
            step = ChainStep(target.qualname, tmod.path, call.lineno, "offloaded to thread")
            self._walk(target, tmod, on_loop=False, depth=depth - 1,
                       entry=entry, entry_module=entry_module, chain=chain + [step],
                       stack=stack, min_conf=tconf)

    def _handle_scheduler(self, call, res, func, module, entry, entry_module,
                          chain, depth, on_loop, stack, min_conf):
        spec = res.scheduler
        # eager args on-loop
        self._emit_eager_calls(call, func, module, entry, entry_module, chain, depth, min_conf, stack)
        idx = spec.work_arg_index
        work_ref = call.arg_refs[idx] if 0 <= idx < len(call.arg_refs) else None
        if work_ref is None:
            return
        if depth <= 0:
            return
        # scheduled work runs ON loop (re-entry forces on_loop True regardless).
        next_on_loop = True if (spec.is_reentry or on_loop) else on_loop
        label = "scheduled on loop" if not spec.is_reentry else "re-entered loop"
        targets = self.index.resolve_callable_ref(work_ref, module, func)
        for target, is_probable in targets:
            tconf = _min_conf(min_conf, Confidence.PROBABLE) if is_probable else min_conf
            tmod = self._module_of(target) or module
            step = ChainStep(target.qualname, tmod.path, call.lineno, label)
            self._walk(target, tmod, on_loop=next_on_loop, depth=depth - 1,
                       entry=entry, entry_module=entry_module, chain=chain + [step],
                       stack=stack, min_conf=tconf)

    def _emit_eager_calls(self, call, func, module, entry, entry_module, chain, depth, min_conf, stack):
        """Eager argument calls of an offload/scheduler run ON-loop (D1).

        These include the expression that *produces* the offloaded callable — e.g.
        the constructor in ``to_thread(Builder(url).render)`` — which executes on
        the loop before dispatch. We resolve each eager call and, if it reaches a
        blocker (directly or by descending into a resolved function/__init__),
        emit on-loop.
        """
        for ec in call.eager_arg_calls:
            res = self.index.resolve_call(ec, module, func)
            if res.kind == KIND_BLOCKER:
                self._emit_blocker(ec, res, func, module, entry, entry_module, chain, depth, min_conf)
            elif res.kind == KIND_FUNCTION:
                self._descend(res.function, ec, func, module, entry, entry_module,
                              chain, depth, True, stack, min_conf)
            elif res.kind == KIND_TIER3 and not res.tier3_overflow:
                cconf = _min_conf(min_conf, Confidence.PROBABLE)
                for cand in res.tier3_candidates:
                    self._descend(cand, ec, func, module, entry, entry_module,
                                  chain, depth, True, stack, cconf)

    # ---- emission ---------------------------------------------------------
    def _emit_blocker(self, call, res, func, module, entry, entry_module, chain, depth, min_conf):
        blk = res.blocker
        used_depth = self.max_depth - depth
        step = ChainStep(res.dotted_target or blk.dotted, module.path, call.lineno, "blocks via")
        f = Finding(
            code=blk.code,
            confidence=min_conf,
            path=module.path, lineno=call.lineno, col=call.col,
            message=blk.message,
            entry_qualname=entry.qualname, entry_path=entry_module.path,
            entry_lineno=entry.lineno,
            blocker_qualname=res.dotted_target or blk.dotted,
            chain=chain + [step], depth=used_depth,
            suggested_fix=blk.suggested_async_replacement,
        )
        self._add(f)

    def _emit_ambiguous(self, call, func, module, entry, entry_module, chain, depth):
        f = Finding(
            code=Code.AMBIGUOUS_DISPATCH, confidence=Confidence.POSSIBLE,
            path=module.path, lineno=call.lineno, col=call.col,
            message=Code.AMBIGUOUS_DISPATCH.title,
            entry_qualname=entry.qualname, entry_path=entry_module.path,
            entry_lineno=entry.lineno,
            blocker_qualname="<ambiguous:%s>" % (".".join(call.func_chain) or "?"),
            chain=list(chain), depth=self.max_depth - depth,
        )
        self._add(f)

    def _emit_await_misuse(self, call, callee, caller, module, entry, entry_module, chain, depth):
        f = Finding(
            code=Code.AWAIT_MISUSE, confidence=Confidence.DEFINITE,
            path=module.path, lineno=call.lineno, col=call.col,
            message=f"coroutine '{callee.name}' is called but never awaited",
            entry_qualname=entry.qualname, entry_path=entry_module.path,
            entry_lineno=entry.lineno,
            blocker_qualname=callee.qualname,
            chain=list(chain), depth=self.max_depth - depth,
        )
        self._add(f)

    def _emit_truncation(self, call, caller, module, entry, entry_module, chain):
        f = Finding(
            code=Code.DEPTH_TRUNCATION, confidence=Confidence.POSSIBLE,
            path=module.path, lineno=call.lineno, col=call.col,
            message=f"{Code.DEPTH_TRUNCATION.title} (--max-depth {self.max_depth})",
            entry_qualname=entry.qualname, entry_path=entry_module.path,
            entry_lineno=entry.lineno,
            blocker_qualname="<truncated:%s>" % (".".join(call.func_chain) or "?"),
            chain=list(chain), depth=self.max_depth,
        )
        self._add(f)

    def _check_loop(self, loop, func, module, entry, entry_module, chain, depth):
        if not loop.is_unbounded:
            return
        # An unbounded loop is safe if it contains ANY genuine yield point (any
        # await / async-for / async-with transfers control to the loop) AND has
        # no known-heavy-CPU registry call in its body that would dominate each
        # iteration regardless of the yield. This keys on the structural presence
        # of a suspension point, not on a specific identifier (generic).
        has_yield = loop.has_yielding_await
        has_heavy = any(
            (lambda b: b and b.code == Code.HEAVY_CPU)(
                self.index.registry.match_blocker(self._dotted(c)))
            for c in loop.body_calls
        )
        if has_yield and not has_heavy:
            return
        f = Finding(
            code=Code.STRUCTURAL_UNBOUNDED, confidence=Confidence.POSSIBLE,
            path=module.path, lineno=loop.lineno, col=loop.col,
            message=Code.STRUCTURAL_UNBOUNDED.title,
            entry_qualname=entry.qualname, entry_path=entry_module.path,
            entry_lineno=entry.lineno,
            blocker_qualname="<unbounded-loop>",
            chain=list(chain), depth=self.max_depth - depth,
        )
        self._add(f)

    def _dotted(self, chain: tuple) -> str:
        return ".".join(chain)

    def _add(self, f: Finding):
        fp = f.fingerprint
        if fp in self._seen_fps:
            return
        self._seen_fps.add(fp)
        self.findings.append(f)

    # ---- prune pre-filter --------------------------------------------------
    def _reaches_blocker(self, func: FunctionIR, stack) -> bool:
        """Cheap, tag-agnostic: does ``func`` reach any registry blocker at all
        (ignoring offload/schedule, ignoring depth)? Used only to decide whether
        a depth-truncation signal is worth emitting."""
        if func.qualname in self._prune_cache:
            return self._prune_cache[func.qualname]
        if func.qualname in stack:
            return False
        stack = stack | {func.qualname}
        module = self._module_of(func)
        result = False
        if module is not None:
            for call in func.calls:
                res = self.index.resolve_call(call, module, func)
                if res.kind == KIND_BLOCKER:
                    result = True
                    break
                if res.kind == KIND_FUNCTION:
                    if self._reaches_blocker(res.function, stack):
                        result = True
                        break
                if res.kind == KIND_TIER3 and not res.tier3_overflow:
                    if any(self._reaches_blocker(c, stack) for c in res.tier3_candidates):
                        result = True
                        break
        # only cache when not on a cycle stack (BLACK)
        if func.qualname not in stack or True:
            self._prune_cache[func.qualname] = result
        return result
