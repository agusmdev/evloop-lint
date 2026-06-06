# evloop-lint

A static analysis tool that detects synchronous, event-loop-blocking calls reachable from `async` code in FastAPI (and async-Python) projects — including deeply nested and indirect cases that existing flat lint rules miss. Shipped as a standalone CLI (`uvx evloop-lint`), with a detection algorithm developed through an adversarial agent loop that continuously tries to break and then harden it.

## Language

**evloop-lint**:
The tool itself. A standalone Python CLI, not a ruff or flake8 plugin (ruff has no plugin API; flake8 plugins cannot be loaded by ruff).
_Avoid_: the plugin, the ruff rule

**Blocking call**:
A synchronous operation that, when executed inside a coroutine running on the asyncio event loop, prevents the loop from progressing until it returns. Scope: (1) known blocking I/O primitives, (2) known-heavy CPU calls, both via the registry; (3) structurally-unbounded compute (low-confidence tier); (4) await/coroutine misuse (adjacent).
_Avoid_: slow call, sync call (a sync call is only a problem if it blocks)

**Known-blocker registry**:
The single auditable data file listing specific blocking primitives — sync I/O (`time.sleep`, `requests.get`, `socket.recv`, sync DB drivers) and known-heavy CPU (`bcrypt.hashpw`, `hashlib.pbkdf2_hmac`, `zlib.compress`). The ONLY place specific identifiers are allowed; traversal logic stays name-agnostic.
_Avoid_: blocklist, blacklist, the list

**Confidence tier**:
The certainty band of a finding, mapped to the resolution mechanism that produced it — `definite` (call resolved through real definitions to a registry blocker; shown by default), `probable` (confident heuristic method-match to a tainting function; opt-in), `possible` (structural-unbounded compute or weak/partial resolution; opt-in). Each tier is its own benchmark track.
_Avoid_: severity, priority, level

**Structural-unbounded finding**:
A low-confidence finding keyed on a loop being unbounded (`while True`, iterating an unknown-length input) AND containing no `await` yield point — a generic, magnitude-free signal of a possible CPU block. Hidden by default; shown only at `--confidence=possible`; scored on a separate benchmark track so it never affects core precision.
_Avoid_: big loop, slow loop, heavy computation

**Reachable-from-async**:
The property that a blocking call sits on some call path that originates in a coroutine and runs on the event loop without an intervening offload (e.g. `run_in_executor`, `anyio.to_thread`). The core thing the detector decides.
_Avoid_: nested call, inside async

**Tainting function**:
A function (sync or async) that transitively reaches a known blocking call within the analysis depth limit, and therefore makes its async callers unsafe.
_Avoid_: dirty function, bad function

**Analysis depth**:
The maximum number of call hops the detector follows from an async function before giving up. Parametric via `--max-depth`, default 4. Bounds worst-case cost and is the loop's primary tuning knob.
_Avoid_: nesting level, recursion limit

**Adversarial loop**:
The development process: a breaker agent writes async code containing blocking calls the detector currently misses (false negatives) or innocent code it wrongly flags (false positives); a fixer agent then improves the detector. Repeated indefinitely. Governed by a fitness function (precision + recall + speed) over a growing labeled benchmark.
_Avoid_: the test loop, fuzzing

**Benchmark**:
The permanent, version-controlled set of labeled cases (`must_flag` blockers + `must_not_flag` safe code) the detector is scored against every iteration. Only grows; never silently regresses (a ratchet).
_Avoid_: test cases, fixtures, the corpus (the corpus is the separate set of real repos)

**Corpus**:
The collection of large real open-source FastAPI repos (e.g. Polar, Langflow, Dispatch) used as a realism and precision guardrail.
_Avoid_: dataset, samples

**Sibling variant**:
A case the judge auto-generates expressing the *same underlying trick* as a breaker case but with a different surface form (other decorator, library, nesting shape). A fix must catch the whole sibling family or it is rejected as a forbidden particular-case patch.
_Avoid_: duplicate, mutation

**Particular-case patch**:
A fix that pattern-matches one specific identifier (`if name == "cached_property"`) instead of the general mechanism. Forbidden. Detected by the sibling-variant holdout.
_Avoid_: hack, special case
