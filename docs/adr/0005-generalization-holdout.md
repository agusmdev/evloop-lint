# Enforce genericity via sibling-variant generalization holdout

The hard mandate is: stay generic, never attack particular cases. This needs a mechanical test, not good intentions — otherwise the fixer wins benchmark points with brittle `if name == "specific_thing"` patches that the next variant walks past.

## Decision

For every case the breaker submits, the judge auto-generates 3–5 **sibling variants** that express the *same underlying mechanism* with a different surface form (a different decorator, a different library, a different nesting shape). A fix is only accepted if it catches the **entire sibling family**. A fix that catches the submitted case but misses siblings is rejected as a particular-case patch.

## Why

This operationalizes "generic" as a pass/fail gate. The fixer cannot pattern-match a single identifier, because siblings using other identifiers for the same trick are scored in the same round. It converts a subjective code-review judgment into an automated test.

## Consequences

- The judge must be capable of producing faithful, *realistic* siblings — bad siblings (unrealistic or expressing a different mechanism) would corrupt the gate, so sibling generation is itself held to the realism bar (ADR-0004).
- It pressures the detector architecture toward a clean split — specific names belong only in an auditable known-blocker registry (data); traversal/resolution logic stays name-agnostic — because only name-agnostic logic survives the sibling holdout. We adopt that split as the natural consequence rather than a separately enforced rule.
- Sibling families are added to the benchmark too, enriching coverage permanently.
