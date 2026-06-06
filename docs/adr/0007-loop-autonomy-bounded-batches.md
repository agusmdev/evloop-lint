# Adversarial loop runs in bounded batches of 20 rounds

The self-improving loop costs real compute per cycle (breaker, sibling-generator, judge-labeler, fixer, benchmark runs). Rather than running unbounded "until convergence" or fully manually, it runs in **bounded batches of 20 break/fix rounds**, then stops and produces a report (what broke, what was fixed, precision/recall/speed deltas per track, new benchmark cases added, any regressions caught). The human approves before the next batch.

## Why

This is a budget/risk-appetite decision, not an engineering one. Bounded batches give predictable cost, keep the human in control, and surface drift or degeneration early — while still accumulating substantial hardening per batch. It honors the "infinite self-improving" spirit (batches can be chained indefinitely) without an open-ended autonomous spend.

## Consequences

- The loop orchestrator must emit a structured end-of-batch report and pause for approval.
- Each batch must leave the benchmark and detector in a committed, consistent state (so a batch is a safe checkpoint).
- A per-round cap and a per-batch round count (20) are configuration, not hardcoded logic.
