"""Threads: the conversations a Project's Turns belong to (agent design §2.2 `threads`).

For now this module holds only the record `resolve` appends to a Thread.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Resolution"]


@dataclass(frozen=True)
class Resolution:
    """What one `resolve` call decided, so the next Turn knows (agent design §6.4 step 5).

    `at` is an RFC 3339 UTC time; the tuples hold Hunk ids. Stale Hunks were
    not applied and stay undecided.
    """

    proposal_id: str
    at: str
    applied: tuple[str, ...]
    rejected: tuple[str, ...]
    stale: tuple[str, ...]
