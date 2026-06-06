"""Rule codes and confidence tiers (D6, D9).

A rule *code* names the mechanism class of a finding. Confidence is an
*orthogonal* axis (D9): the same code can be emitted at different tiers
depending on how the chain was resolved.

Codes are stable forever — baselines, fingerprints and the benchmark depend on
them. Which *library* triggered a finding lives in the message, never the code.
"""

from __future__ import annotations

from enum import Enum


class Confidence(str, Enum):
    """Resolution-mechanism-derived certainty band (ADR-0006)."""

    DEFINITE = "definite"   # resolved through real defs to a registry blocker
    PROBABLE = "probable"   # confident Tier-3 heuristic method match
    POSSIBLE = "possible"   # structural / weak / partial resolution

    @property
    def rank(self) -> int:
        return {"definite": 3, "probable": 2, "possible": 1}[self.value]


# Ordered floors: a floor admits its own tier and everything more certain.
_CONF_ORDER = [Confidence.POSSIBLE, Confidence.PROBABLE, Confidence.DEFINITE]


def confidence_at_or_above(tier: Confidence, floor: Confidence) -> bool:
    return tier.rank >= floor.rank


class Code(str, Enum):
    """EVL rule codes (D9)."""

    PARSE_ERROR = "EVL000"          # parse error / skipped file (informational)
    SYNC_IO = "EVL001"             # sync blocking I/O primitive
    HEAVY_CPU = "EVL002"           # known-heavy CPU primitive
    STRUCTURAL_UNBOUNDED = "EVL003"  # unbounded loop, no yielding await (possible)
    AWAIT_MISUSE = "EVL004"        # un-awaited coroutine / await misuse
    DEPTH_TRUNCATION = "EVL005"    # potential blocker past --max-depth (possible)
    AMBIGUOUS_DISPATCH = "EVL006"  # tier-3 fanout / dynamic dispatch (possible)
    SYNC_DB_DRIVER = "EVL011"      # sub-kind of sync I/O: sync DB driver call

    @property
    def title(self) -> str:
        return _TITLES[self]


_TITLES = {
    Code.PARSE_ERROR: "file could not be parsed",
    Code.SYNC_IO: "blocking I/O call on the event loop",
    Code.HEAVY_CPU: "CPU-heavy call on the event loop",
    Code.STRUCTURAL_UNBOUNDED: "unbounded loop with no yield point on the event loop",
    Code.AWAIT_MISUSE: "coroutine is never awaited",
    Code.DEPTH_TRUNCATION: "potential blocker reachable past --max-depth",
    Code.AMBIGUOUS_DISPATCH: "ambiguous or dynamic dispatch may reach a blocker",
    Code.SYNC_DB_DRIVER: "blocking database driver call on the event loop",
}

# Default tier a code is *capable* of emitting at its strongest. Used for the
# --select/--confidence orthogonality warning (D7): selecting a possible-only
# code under --confidence=definite would otherwise silently return nothing.
POSSIBLE_ONLY_CODES = frozenset(
    {Code.STRUCTURAL_UNBOUNDED, Code.DEPTH_TRUNCATION, Code.AMBIGUOUS_DISPATCH}
)

# Which codes cause a non-zero CI exit by default: only definite-capable ones,
# and only when actually emitted at/above the active confidence floor (D7).
DEFAULT_FAILING_CODES = frozenset(
    {Code.SYNC_IO, Code.HEAVY_CPU, Code.AWAIT_MISUSE, Code.SYNC_DB_DRIVER}
)
