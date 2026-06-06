# Loop governance: stop condition, quality floors, and corpus gating

These are the product-posture decisions that govern the adversarial loop (made by the human; the
engineering panel deliberately deferred them). They sit on top of ADR-0004 (dual-metric fitness),
ADR-0007 (20-round batches), and D10 (orchestration).

## Stop condition — convergence with a hard backstop

Chaining of 20-round batches stops when a batch adds fewer than K new `must_flag` cases AND the
precision/recall floors have been unchanged for M consecutive batches (diminishing returns). A hard
per-batch wall-clock + spend cap is always enforced regardless, so a single batch can never run away.
This honors the "infinite self-improving" spirit (batches chain indefinitely until they stop paying off)
while bounding cost. (K and M are configuration.)

## Quality floors — precision-first, ratcheting recall

- **definite track: precision floor ≈ 99%** — a hard gate. Precision is the adoption currency
  (ADR-0003); the green result must be trustworthy.
- **every track: a steadily-ratcheting recall floor** — locked and only increasing, so the
  FN-mining corpus guardrail (D10) has real teeth and the loop cannot drift toward silence.
- **probable / possible tracks: looser precision** is acceptable because those findings are opt-in.

## Corpus gating — precision gate + dynamic-dispatch coverage floor

- **Corpus precision is a hard gate:** a fix that introduces a false positive on real code (Polar,
  Langflow, Dispatch) is rejected.
- **A minimum dynamic-dispatch coverage metric (EVL006) is required:** the tool cannot pass a batch as
  "success" while architecturally blind to a heavily dynamic repo (Langflow's `getattr`/dict-of-callables
  style). This keeps "high precision, near-zero recall" from masquerading as a good result.

## Consequence

The fitness function is now fully specified as hard gates (definite precision ≥99%, ratcheting per-track
recall floors, corpus precision, EVL006 coverage minimum) plus the convergence stop rule — every fixer
diff must clear all of them before acceptance.
