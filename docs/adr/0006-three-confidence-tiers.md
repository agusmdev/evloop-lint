# Three confidence tiers mapped to resolution mechanism

Findings are emitted in one of three confidence tiers, and crucially each tier corresponds to *how* the finding was derived, not a hand-tuned guess:

- **definite** — the call was resolved through real definitions (import/name resolution, Tiers 1–2) all the way to a known-blocker registry entry. Shown by default. Precision-critical track.
- **probable** — the call reached a tainting function via the custom resolver's confident heuristic method-match (Tier 3, `instance.method()` without true type inference). Hidden unless `--confidence=probable`. For stricter CI.
- **possible** — structural-unbounded compute (unbounded loop, no await yield point) or weak/partial resolution. Hidden unless `--confidence=possible`. Separate track so it never affects core precision.

## Why

A two-tier model was tempting for simplicity, but the custom resolver (ADR-0002) produces a genuinely distinct middle class — heuristic method matches — that is more trustworthy than a structural guess yet less certain than a fully resolved chain. Giving it its own tier keeps each tier tied to a *resolution mechanism* (which is generic) rather than an arbitrary score, and lets CI dial strictness.

## Consequences

- The benchmark is partitioned into three tracks; each scored for precision/recall independently so a regression in `possible` cannot mask progress in `definite`.
- CLI: `--confidence={definite|probable|possible}` selects the floor (default `definite`). Higher tiers are supersets downward.
- Exit-code / CI semantics must specify which tier(s) fail the build (see later CLI decision).
