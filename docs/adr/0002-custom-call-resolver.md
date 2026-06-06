# Build a custom call resolver instead of vendoring astroid/PyCG

To follow calls across files, evloop-lint must resolve a call name/attribute to its definition. Mature options exist: astroid (pylint's inference engine, resolves instance method calls via type inference + MRO) and PyCG/pycg-rs (off-the-shelf whole-project call graphs, but ~70% recall).

We chose to build our own resolver: import graph, module-alias tracking, name→definition mapping (Tiers 1–2 statically), and heuristic matching for instance method calls (Tier 3).

## Why

The resolver is the core IP that the adversarial loop iterates on. If a vendored engine misses an edge (PyCG's ~30% false-negative rate) or mis-infers a type, the breaker agent's win lands in third-party code we cannot cleanly fix, and the loop stalls. Owning the resolver means every break/fix cycle hardens *our own* artifact. The reinvention cost is precisely the work the loop is built to grind through.

## Consequences

- We must handle Tier 3 (`instance.method()`) without true type inference — initially via name-based heuristics (match method name across known classes), accepting some imprecision the loop will tune.
- Tier 4 (dynamic dispatch: `handlers[key]()`, `getattr(o,n)()`) is statically undecidable; we need an explicit policy for it (see later decision).
- No heavy runtime dependency; faster startup, simpler `uvx` packaging.
