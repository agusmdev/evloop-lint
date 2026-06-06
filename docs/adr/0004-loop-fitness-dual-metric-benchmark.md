# Adversarial loop scored by dual metrics over a growing labeled benchmark

The loop is not a bare duel. It is governed by a fitness function over a permanent, labeled benchmark.

## Decision

- Maintain a benchmark of labeled cases in two buckets: `must_flag` (code that genuinely blocks the loop and should be detected) and `must_not_flag` (idiomatic safe code, including realistic unresolved/third-party/dynamic calls).
- Each iteration is scored on **precision AND recall AND speed simultaneously**, against this benchmark (and the real-repo corpus — see ADR-0005).
- The **breaker** proposes NEW realistic cases. Accepted cases are labeled and added to the benchmark **permanently** (a ratchet — no later iteration may regress them).
- The **fixer** must raise recall **without** dropping precision or speed below defined thresholds. A change that gains a `must_flag` but loses a `must_not_flag` is rejected.

## Why

An unscored loop degenerates. Three failure modes this prevents:
1. **Flag-everything** ("unbreakable" but useless): precision scoring blocks it.
2. **Unrealistic breaker code** (`eval`, base64 `__import__`): such cases are rejected at labeling time as not idiomatic, enforcing the "no particular cases / stay generic" mandate.
3. **Overfitting to the breaker**: the permanent benchmark + corpus regression rewards general fixes, and a fix that only patches one example without generalizing tends to be re-broken by the next realistic variant.

## Consequences

- The benchmark is the project's most valuable asset; it must be version-controlled and only grow.
- Labeling is a real step (human or a disciplined judge agent), not automatic — a breaker case is only `must_flag` if it's realistic AND genuinely blocking.
- Convergence is monotonic: score can only improve or hold, never silently regress.
