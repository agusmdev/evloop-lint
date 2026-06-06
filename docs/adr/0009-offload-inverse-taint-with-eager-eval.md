# Offloads are inverse-taint scoped to the work-bearing argument, with eager-eval and re-entry edges

Offload primitives (`loop.run_in_executor`, `asyncio.to_thread`, `anyio.to_thread.run_sync`,
`run_in_threadpool`) are modeled as an **off-loop edge scoped to one argument** — the work-bearing
callable (registry row names the arg index per primitive). Only that callable's *body* runs off-loop;
everything else stays on-loop.

## Why this precise scoping (the subtle part)

A naive "this call is an offload, so its blocker is fine" is wrong three ways, all found by adversarial
review:
1. **Eager-evaluation:** the expression that *builds* the callable runs on the loop *before* the thread
   starts. `Worker(requests.get(x)).run` offloads `run`, but `requests.get(x)` and the `Worker(...)`
   constructor execute on-loop and must be flagged. Only the resolved callable's body is off-loop.
2. **Wrapper bound args:** `functools.partial(f, requests.get(x))` offloads `f`'s body, but the bound
   argument `requests.get(x)` is evaluated on-loop. Wrapper calling-conventions (which slots are
   eager args vs the target) are registry data.
3. **Re-entry:** `anyio.from_thread.run` / `asyncio.run_coroutine_threadsafe` push work *back onto* the
   loop from inside a worker. They are `schedule_on_loop` edges that re-set `on_loop=True` even inside an
   off-loop subtree; `schedule_on_loop` always wins over an enclosing offload.

Also: offloading an `async def` does NOT suppress await-misuse (EVL004) — offload tags suppress
on-loop-blocker findings only; await-misuse is loop-agnostic.

## Generic-safe

Primitive names, work-bearing-arg indices, wrapper eager-slot maps, and re-entry tags are all registry
data. Eager-evaluation and re-entry are name-agnostic structural rules (arg vs callable body; which tag a
resolved edge carries). New offload libraries / wrapper styles are registry rows, never logic changes —
they survive the sibling-variant holdout (ADR-0005).

## Consequence

The taint walk carries an `on_loop` context flag that flips off entering an offloaded callable body and
back on at a re-entry edge — making "off-loop" the exact structural inverse of blocker-taint, and the
inverse-trap (a sibling blocker on the line after an offload still counts) falls out for free.
