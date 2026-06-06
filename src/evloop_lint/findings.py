"""Finding model + fingerprint (D6)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .codes import Code, Confidence


@dataclass
class ChainStep:
    """One hop in the blocking chain (entry -> ... -> blocker)."""

    qualname: str
    path: str
    lineno: int
    label: str = ""        # e.g. "async entry", "calls", "blocks via"


@dataclass
class Finding:
    code: Code
    confidence: Confidence
    path: str               # file of the blocker call site
    lineno: int             # blocker call site line
    col: int
    message: str
    entry_qualname: str     # the async entry the chain started from
    entry_path: str
    entry_lineno: int
    blocker_qualname: str   # dotted blocker (or function) that blocks
    chain: list = field(default_factory=list)   # list[ChainStep]
    depth: int = 0
    suggested_fix: str = ""

    @property
    def fingerprint(self) -> str:
        """Stable identity anchored on the BLOCKER call SITE, not the witness
        chain (D6).

        The blocker's own file+line+code is invariant under resolver changes
        (it is the actual blocker location, not the entry/witness path), so it
        is a stable baseline/suppression anchor — while still distinguishing two
        distinct blocker occurrences of the same primitive in one file.
        """
        basis = f"{self.path}::{self.lineno}::{self.blocker_qualname}::{self.code.value}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def location(self) -> str:
        return f"{self.path}:{self.lineno}:{self.col + 1}"
