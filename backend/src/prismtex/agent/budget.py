"""Turn budgets: the limits of a Turn and the price of a model (agent design §2.2 `budget`).

The guard and the ledger of agent design §11.8 build on these records.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Limits", "Price"]


@dataclass(frozen=True, slots=True)
class Limits:
    """The caps of one Turn (agent design §4.7)."""

    max_steps: int
    max_wall_s: float
    max_tokens: int
    max_tool_calls: int


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens; `cache_write` is the 5-minute cache write rate."""

    input: float
    output: float
    cache_write: float
    cache_read: float
