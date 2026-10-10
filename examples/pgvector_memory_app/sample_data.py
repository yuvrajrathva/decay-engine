"""Deterministic sample memories, one per decay outcome.

``idle_days`` is how long ago the memory was last retrieved; ``age_days``
defaults to 30 days older than that. ``expected`` is what the demo's policy
should decide, and the demo asserts it, so this file doubles as a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Sample:
    id: str
    document: str
    idle_days: Optional[float]  # None means "no timestamps at all"
    retrieval_count: int = 0
    pinned: bool = False
    expected: str = "keep"
    note: str = ""

    @property
    def age_days(self) -> Optional[float]:
        return None if self.idle_days is None else self.idle_days + 30


SAMPLES = (
    Sample(
        id="pref-units",
        document="The user prefers metric units for distance and weight.",
        idle_days=2,
        retrieval_count=12,
        expected="keep",
        note="read two days ago and often; plainly still in use",
    ),
    Sample(
        id="pref-dark-mode",
        document="The user prefers dark mode in the dashboard interface.",
        idle_days=10,
        retrieval_count=3,
        expected="keep",
        note="recent enough to stay strong",
    ),
    Sample(
        id="contact-email",
        document="The user's primary contact email is ravi@example.com.",
        idle_days=500,
        retrieval_count=0,
        pinned=True,
        expected="keep",
        note="pinned, so never touched however cold it gets",
    ),
    Sample(
        id="sso-pricing",
        document="The user asked once about SSO pricing for the team plan.",
        idle_days=60,
        retrieval_count=1,
        expected="demote",
        note="cooling off: flagged cold, kept in the store",
    ),
    Sample(
        id="name-timezone",
        document="The user's name is Ravi and their timezone is Asia/Kolkata.",
        idle_days=500,
        retrieval_count=90,
        expected="demote",
        note="cold enough to evict, but 90 retrievals earn tenure",
    ),
    Sample(
        id="debug-trace",
        document="Debug trace 9f21a4 captured during a one-off support session.",
        idle_days=240,
        retrieval_count=0,
        expected="evict",
        note="never reused; exactly the junk a sweep is for",
    ),
    Sample(
        id="scratch-note",
        document="Scratch note about a draft invoice layout that was abandoned.",
        idle_days=320,
        retrieval_count=2,
        expected="evict",
        note="barely used and long idle; below the tenure threshold",
    ),
    Sample(
        id="legacy-import",
        document="Imported note from the old system, with no timestamps at all.",
        idle_days=None,
        retrieval_count=0,
        expected="skip",
        note="unmeasurable, so reported and left alone -- never deleted",
    ),
)
