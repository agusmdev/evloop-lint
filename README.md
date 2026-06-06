# evloop-lint

[![CI](https://github.com/agusmdev/evloop-lint/actions/workflows/ci.yml/badge.svg)](https://github.com/agusmdev/evloop-lint/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Detect synchronous, **event-loop-blocking** calls reachable from `async` code in
FastAPI / async-Python projects — including the **deeply nested, interprocedural,
cross-file** cases that ruff's flat `ASYNC` rules cannot follow.

```
app/main.py:4:5 EVL001 [definite] time.sleep blocks the event loop
  main.deep_handler [app/main.py:3] (async entry)
  -> service.fetch [app/service.py:7] (calls)
  -> db.query [app/db.py:4] (calls)
  -> time.sleep [app/db.py:4] (blocks via)
  fix: consider asyncio.sleep
```

ruff catches `time.sleep` only when you write it *directly* inside an `async def`.
evloop-lint follows the call across files — router → service → repository → driver
— and reports the whole chain. It is pure-stdlib (no runtime dependencies) and
fast (hundreds of files in well under a second).

## Quick start

The fastest way, with [uv](https://docs.astral.sh/uv/) (no install, runs straight
from this repo):

```bash
uvx --from git+https://github.com/agusmdev/evloop-lint evloop-lint path/to/your/app
```

Or clone and run:

```bash
git clone https://github.com/agusmdev/evloop-lint
cd evloop-lint
uv run --with pytest pytest          # run the test suite (41 tests)
uv run python -m evloop_lint.cli path/to/your/app
```

Or install into your environment:

```bash
pip install git+https://github.com/agusmdev/evloop-lint
evloop-lint path/to/your/app
```

### Try it on a sample in 10 seconds

```bash
mkdir -p demo/app
cat > demo/app/db.py      <<'EOF'
import time
def query():
    time.sleep(1)          # the real blocker, hidden 3 hops deep
EOF
cat > demo/app/service.py <<'EOF'
from app.db import query
def fetch():
    return query()
EOF
cat > demo/app/main.py    <<'EOF'
from app.service import fetch
async def handler():
    fetch()                # ruff sees nothing here — evloop-lint follows the chain
EOF

uvx --from git+https://github.com/agusmdev/evloop-lint evloop-lint demo
```

You should see a single `EVL001` finding with the full
`handler -> fetch -> query -> time.sleep` chain, and a non-zero exit code.

## What it finds

`evloop-lint` builds a project-wide call graph and propagates "reaches a blocking
call" taint backward from every `async def` entry point, carrying an on-loop /
off-loop context so that **correctly-offloaded** work (`asyncio.to_thread`,
`loop.run_in_executor`, `anyio.to_thread.run_sync`, `run_in_threadpool`, …) is
*not* flagged. It understands schedulers (`call_soon`, `create_task`), re-entry
(`anyio.from_thread.run`), `functools.partial`, constructor `__init__` bodies,
`@property` getters, and FastAPI's threadpool semantics for plain `def` endpoints.

## Rule codes

| Code | Meaning | Tier(s) |
|------|---------|---------|
| `EVL001` | blocking I/O call on the loop | definite / probable |
| `EVL002` | CPU-heavy call on the loop | definite / probable |
| `EVL003` | unbounded loop, no yield point | possible |
| `EVL004` | coroutine never awaited | definite |
| `EVL005` | potential blocker past `--max-depth` | possible |
| `EVL006` | ambiguous / dynamic dispatch | possible |
| `EVL011` | blocking DB driver call | definite / probable |

## Confidence tiers

Findings are emitted at a tier matching *how* the chain was resolved:

- **`definite`** — resolved through real definitions to a known blocker. Shown by
  default; fails CI.
- **`probable`** — confident heuristic method match (e.g. `self.repo.find()`).
  Opt-in: `--confidence=probable`.
- **`possible`** — structural / weak / partial resolution. Opt-in:
  `--confidence=possible`.

The tool is **optimistic**: a call it cannot resolve is assumed safe, keeping the
false-positive rate near zero so the default run stays trustworthy.

## CLI flags

```
--max-depth N          max call hops to follow (default 4)
--confidence TIER      minimum tier to report (definite|probable|possible)
--format FMT           text | json | ndjson | sarif | github
--select CODES         only these rule codes (comma-separated)
--ignore CODES         exclude these rule codes
--exclude GLOBS        path globs to skip
--statistics           coverage + depth-truncation stats
--no-framework-detect  treat every async def as on-loop (max recall)
--strict               parse errors cause a non-zero exit
--exit-zero            always exit 0 (report only)
```

Exit codes: `0` no findings at/above the floor · `1` findings found · `2` usage error.

Suppress a line with `# noqa` or `# noqa: EVL001`.

## Configuration

Via `pyproject.toml`:

```toml
[tool.evloop-lint]
max-depth = 4
confidence = "definite"
ignore = ["EVL003"]
exclude = ["tests/*", "migrations/*"]
```

## CI integration

```yaml
- name: Check for event-loop blockers
  run: uvx --from git+https://github.com/agusmdev/evloop-lint evloop-lint app/
```

SARIF output (`--format sarif`) uploads to GitHub code scanning; `--format github`
emits inline PR annotations.

## How it works / design

The detector is deliberately **generic**: every specific identifier (blocking
primitives, offload primitives, framework registration shapes, wrappers) lives in
a data registry (`src/evloop_lint/registry.py`), never in traversal logic. New
libraries are data rows, not code changes.

It was developed through an **adversarial loop**: breaker agents generate realistic
FastAPI code that tries to evade detection, a judge labels true escapes, and each
escape is fixed *generically* and added as a permanent regression test
(`tests/test_adversarial.py`). See [`docs/DESIGN.md`](docs/DESIGN.md) for the full
algorithm (D1–D10) and [`docs/adr/`](docs/adr/) for the key decisions.

## License

MIT — see [LICENSE](LICENSE).
