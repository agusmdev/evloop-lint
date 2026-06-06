# Optimistic verdict: a call is safe unless proven to reach a blocker

When the resolver cannot determine what a call does (unknown instance type, third-party library not analyzed, dynamic dispatch), evloop-lint treats it as **safe** rather than flagging it.

## Why

A linter is only valuable if it's adopted, and adoption dies on false positives — a noisy tool gets blanket-suppressed or uninstalled. Optimism keeps precision high and noise low. It also gives the adversarial loop a clean, productive job: every blocker the breaker successfully hides behind an unresolved call becomes new, *specific* knowledge (a known-blocker signature, a resolution improvement) that the fixer encodes — so recall climbs over time while precision stays pinned near 100%.

## Consequences

- Recall starts lower and is grown deliberately by the loop, rather than faked by flagging everything.
- This makes pure false-negative scoring dangerous: an optimistic tool trivially minimizes false positives, so the loop MUST also score false negatives, and a counter-breaker MUST attack false positives, or the tool degenerates (see ADR-0004).
- We will maintain curated knowledge of known blocking calls (stdlib + popular libs) as the seed of "proven blocking."
