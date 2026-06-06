# Unresolved framework roles default OFF the event loop (uniform optimism)

When evloop-lint cannot prove a function's framework role (e.g. a router returned from a factory whose
type it can't resolve, a decorator receiver it can't pin to a framework class, an ambiguous `.get` shape),
it disposition the function as **off-loop / no overlay** — never on-loop.

## Why this is surprising

The intuitive default for a *blocking* linter is "if unsure, assume it runs on the loop and flag it."
We do the opposite. ADR-0003 makes optimism the universal rule, and entry-point detection is exactly
where violating it would hurt most: FastAPI/Starlette apps build routers through factories and helpers
that a static resolver frequently cannot follow. Defaulting those to on-loop would false-positive on the
single most common real-world pattern (Polar, Langflow) and kill adoption.

## How recall is preserved

Off-loop-on-uncertainty would be a false-negative amnesty if left alone. It isn't, because of
**dual-context analysis**: the same function reached *directly* (not via a decorator/`Depends` edge) from
a proven on-loop frame is still analyzed with `on_loop=True` in that context (the taint memo key includes
`on_loop`). So a blocker inside a plain `def` is still caught whenever any async caller reaches it
directly — the decorator-resolution failure only suppresses the *endpoint-entry* path, not the
direct-call path.

## Generic-safe

Disposition is by structural shape matched against a framework registry shape-DSL
(`decorator-attr-call | bare-decorator | positional-registration | dependency-callable`). Zero verb or
framework names live in matcher logic; a new framework is a data row. `--no-framework-detect` drops to
the pure language-semantic layer (every `async def` on-loop, no subtraction).
