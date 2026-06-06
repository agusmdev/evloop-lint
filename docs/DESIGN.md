# evloop-lint — Technical Design (D1–D10)

Decisions made by an adversarial senior-engineer panel (propose → synthesize → break → harden),
consistent with ADR-0001…0007 and the genericity mandate. Specific identifiers live ONLY in
data registries; all traversal/resolution/output logic is name-agnostic.

## D1 — Offload escape hatches (argument-scoped, eager-eval + re-entry hardened)
Offload is an edge property scoped to the **work-bearing argument** (registry row per primitive
names the arg index: `run_in_executor`=1, `to_thread`/`run_sync`/`run_in_threadpool`=0). Recurse
into that one callable's subtree with `on_loop=False`; all other args and every lexically-later
statement stay on-loop.
- **Eager-evaluation rule:** any expression that *produces* the work-bearing callable (receiver of a
  bound method, constructor calls, decorators at the call site, walrus/f-string subexprs in the arg
  list) is evaluated ON-loop — only the resolved callable's *body* is off-loop.
- **Partial/lambda unwrap:** wrapper bound-arg expressions stay on-loop (`partial(f, requests.get(x))`
  flags `requests.get`); only `f`'s body is off-loop. Wrapper calling-convention is a registry row.
- **Re-entry edge:** `anyio.from_thread.run` / `asyncio.run_coroutine_threadsafe` are `schedule_on_loop`
  rows that re-set `on_loop=True` even inside an off-loop subtree; `schedule_on_loop` always wins over
  an enclosing offload context.
- Offloading an `async def` does NOT suppress EVL004 (await-misuse is loop-agnostic).
- Unresolved callable arg → optimistic off-loop (ADR-0003).

## D2 — Entry points (uniform optimism; unresolved shape defaults OFF-loop; recall via direct-call)
- **Layer 1 (language fact):** every `async def` is an on-loop root; a sync callee reached from an
  on-loop frame inherits the loop via a `follow_on_loop` edge.
- **Layer 2 (registry overlay, subtractive):** a `def`/`async def` is dispositioned by **structural
  shape**, not name, via a framework shape-DSL (`decorator-attr-call | bare-decorator |
  positional-registration | dependency-callable`). HTTP verbs, `api_route`/`websocket` names,
  `Depends`/framework-class names, and registration styles (aiohttp `app.router.add_get(path, handler)`,
  Litestar bare `@get()`) are all DATA.
- **Keystone fix:** when a framework role CANNOT be resolved, default **off-loop / no-overlay** —
  uncertainty is optimistic everywhere (never on-loop), so we never FP on a possibly-threadpooled
  handler.
- **Recall recovery:** the same function reached *directly* (not via a decorator/Depends edge) from an
  on-loop frame is analyzed on-loop in that context (dual-context; D4 key includes `on_loop`). A blocker
  in a plain `def` is caught whenever any async caller reaches it directly.
- `--no-framework-detect` → Layer-1 only (max recall, every `async def` on-loop).

## D3 — AST foundation (stdlib `ast`; raw-facts IR; range-aware suppression; highest-Python pin)
Stdlib `ast`. One visitor pass per file emits a compact **picklable IR**; the AST is discarded after.
- Decorator shapes stored as **raw structural facts**; the IR makes zero verb/endpoint judgments
  (registry decides role at link time).
- **Suppression matches the full line RANGE** `[node.lineno, node.end_lineno]` so `# noqa` on a
  multi-line call's closing-paren line works. `# noqa` harvested via `tokenize` in the same pass.
- **Pin to the HIGHEST Python analyzed** (≥3.12 for the corpus) so PEP 695 / new match syntax parse.
  A `SyntaxError` emits one `EVL000` and skips the file, but the **skipped-file ratio is a coverage-loss
  signal** gated in the loop (rising ratio = regression; skipped files are silent FNs).

## D4 — Call-graph & taint (context-keyed; depth surfaced; Tier-3 bounded by tier)
Three phases: Collect (parallel) / Link (serial) / Taint (memoized).
- **Soundness:** the off-loop-agnostic `reaches_blocker` cache is a **prune-only pre-filter**, never a
  leaf-emit signal. Any subtree containing an edge-tagged call (offload/threadpool/schedule_on_loop/
  wrapper) is re-descended with the `on_loop` context; `reaches_blocker=False` still prunes. Emitting
  memo key = `(def, on_loop)`; edge-tagged subtrees memoize per full context.
- **Depth never swallowed:** at `depth==0`, if `reaches_blocker` is true beyond the cut, emit a
  **possible-tier truncation finding** (EVL005) at the last resolved on-loop site (default CI stays
  quiet; opt-in surfaces it).
- **Tier-3 fanout bounded by confidence, not by dropping edges:** many candidate classes → probable-tier
  edges, capped by a per-call candidate budget; on overflow, downgrade to a single possible-tier
  ambiguous-method signal (EVL006) — never silent.
- Depth decrements per real call hop; offload-arg lookthrough / wrapper unwrap do not.
- Cycles: WHITE/GREY/BLACK on `(def, on_loop)`; GREY back-edge → optimistic default, not cached; only
  BLACK cached.

## D5 — Speed (content+logic-hash cache; process pool; measured spawn tax)
Five layers: parse-once content-hash cache; `ProcessPoolExecutor` for Phase-1; serial lazy Phases 2-3;
memoization; incremental.
- **Cache key = (content_hash, tool_version, python_version, registry_hash, ANALYZER_LOGIC_HASH).** The
  logic hash (build-time hash of resolver/taint/edge-tag modules) auto-invalidates results when the
  fixer changes analysis logic mid-loop, so the acceptance gate never scores stale behavior — while the
  parse cache for unchanged files survives. IR caches on content_hash; RESULTS cache adds the logic hash.
- **Spawn tax measured** (`--statistics` reports pool overhead); `<~50` files → serial; forkserver
  fallback on POSIX where spawn dominates.
- `.gitignore` honored by default (`--include` to override); fingerprints anchored on **repo-relative
  canonical path** (symlink-stable); realpath used only for dedup.

## D6 — Output & suppression (chain trace default; blocker-anchored fingerprints; hints are data)
Human text default WITH the **blocking chain** (entry → … → blocker — the reason to exist over flat
rules); plus `json` (ndjson), SARIF 2.1.0 (chain as codeFlows/threadFlows), github annotations. Finding:
`rule_code, category, confidence, blocker, entry, call_site, chain, depth, max_depth, message,
resolution_path, fingerprint`.
- **Suppression anchored on the BLOCKER line** (invariant under resolver changes); entry-line `# noqa`
  accepted as best-effort. Fingerprint = `(blocker file + qualified_name + code)`, NOT the witness path,
  so a fixer change that shifts the witness path does not rot baselines/suppressions (kills a
  self-inflicted ratchet break).
- **Fix-hints** (`requests`→`httpx.AsyncClient`, `psycopg2`→`asyncpg`) are a per-registry-entry
  `suggested_async_replacement` DATA field; the loop's diff-lint scope includes the output module.
- SARIF threadFlows capped at configurable max length + `--sarif-split`; warn near code-scanning limits.

## D7 — CLI & exit codes (definite-only fails CI; recall made VISIBLE, never silent zeros)
Flags: `--max-depth 4`, `--confidence definite|probable|possible` (default definite), `--format`,
`--select/--ignore`, `--no-chain`, `--fix-hints`, `--jobs`, cache flags, `--exclude/--include`,
`--no-framework-detect`, `--baseline`, `--warn-unused-ignores`, `--statistics`, `--strict`,
`--exit-zero`, `--isolated`, `--config`. Config via `pyproject.toml [tool.evloop-lint]`; precedence
CLI > config > default. Registries config-extensible via the shape-DSL.
- **Exit:** 0 = none at/above floor; 1 = findings at/above floor (CI-fail); 2 = usage/config/internal.
  Parse errors alone → 0 unless `--strict`. **Only DEFINITE fails the build by default.**
- **Visibility fixes:** a default run prints a `--statistics` line ("N potential chains truncated at
  --max-depth 4; run --confidence=possible or raise --max-depth") even when exit 0, so the tool never
  silently looks useless on deep/dynamic code. `--select` on a code that only emits above the active
  floor emits a WARNING, never silent zeros. `--select/--ignore` = category axis; `--confidence` = tier
  axis; both must pass (documented AND warned-on).

## D8 — Async runtime scope (asyncio+anyio first-class; trio via registry; implicit channels modeled)
asyncio + anyio first-class; trio via registry rows only (zero trio core logic). All constructs are edge
tags. Four channel-closing additions:
- **`call_soon` family** (`call_soon`/`call_later`/`call_at`/`call_soon_threadsafe`) = `schedule_on_loop_sync`
  rows: the scheduled SYNC callable is analyzed ON-loop.
- **Implicit-call sites:** iterator + descriptor protocols modeled as synthetic call edges — a `for`/
  comprehension/unpacking over a sync iterable whose `__next__` resolves to a tainting generator/cursor
  is an on-loop edge; an attribute access resolving to a `@property`/descriptor `__get__` is a synthetic
  call edge. Unresolved → optimistic-safe.
- **EVL003 redefined:** an unbounded loop is structural-unbounded (possible) unless it contains an
  `await` to a **registry-known yielding primitive** AND has no known-heavy-CPU registry call in the body
  — so `await asyncio.sleep(0)` no longer launders a CPU loop.
- **BackgroundTasks disposition is VERSIONED DATA** (sync→threadpool/off-loop in current Starlette;
  async→on-loop), carrying the Starlette version range. `create_task`/`ensure_future`/`TaskGroup.start_soon`/
  `gather` = `schedule_on_loop` (on-loop, same loop) — the high-value FastAPI footgun.

## D9 — Rule codes (EVL namespace; EVL003 laundering-resistant; truncation/dispatch codes)
`EVL` namespace; category = code; confidence = orthogonal tier field.
- `EVL001` sync I/O (definite/probable); `EVL002` heavy-CPU (definite/probable); `EVL003`
  structural-unbounded (possible only, redefined per D8); `EVL004` await/coroutine misuse (definite);
  `EVL000` parse/skip (informational, ratio gated).
- `EVL005` (possible) depth-truncation; `EVL006` (possible) ambiguous/dynamic dispatch — the two biggest
  silent-recall-loss channels made into named, opt-in codes.
- EVL004 exempts intentional scheduling via `schedule_on_loop` tags + a name-agnostic "coroutine value
  consumed by a scheduling edge or bound-then-awaited" dataflow check (exemption set = registry tags).
- Reserved ranges: `EVL00x` core, `EVL01x` sub-kinds (`EVL011` sync DB driver), `EVL1xx` framework
  refinements, `EVL9xx` meta. Codes are stable forever (baselines/fingerprints/benchmark depend on them).
  Which library is in the message/registry_id, never the code.

## D10 — Loop orchestration (diff-lint hardened; recall guardrails; diversified siblings; cost measured)
Roles: breaker, judge-labeler, sibling-generator, fixer. Flat version-controlled benchmark
(definite/probable/possible × must_flag/must_not_flag + corpus pointers); `meta.toml` per case
(label, track, rule_code, mechanism_id, origin, holdout, expected_findings). Gate (fail-fast): apply →
new case passes at tier → sibling holdout (incl. reserved unseen siblings) → genericity diff-lint →
full 3-track regression → corpus precision guardrail → speed gate → determinism check. Bounded 20-round
batches + report + human approval (ADR-0007). Four hardening changes:
1. **Diff-lint is not theater:** rejects `if name == 'x'` AND membership tests against in-module literal
   collections (`if method in {'get','post'}`). The only legal home for such a set is a registry/config
   file. Scope includes resolver, taint, edge-tag, AND output modules.
2. **Recall-erosion guard:** a fixer diff that removes/downgrades any previously-emitted benchmark finding
   is rejected unless it adds a compensating `must_flag` the judge ratifies. Resolution can only get more
   precise, never quietly more optimistic.
3. **FN-witness corpus guardrail:** each batch the breaker mines N candidate FNs *from the corpus*; judge
   labels the true ones into `must_flag`. A per-track **recall floor** (locked, ratcheting) sits beside
   the precision floor — recall is a hard gate, not just a reward.
4. **Sibling-generator diversity:** siblings generated along an enumerated axis taxonomy (library swap,
   decorator shape, nesting depth, offload primitive, dispatch style, async-construct, syntactic channel);
   the reserved holdout is drawn from a DIFFERENT axis than fixer-visible siblings. Loop cost (CLI
   process-invocation count + wall-time) measured per batch against ADR-0007's budget.
