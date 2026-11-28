"""The records a Turn hands its Profile (agent design §2.2 `turn`).

`TurnScope` is everything a Profile may touch, and nothing else.
`RepairFeedback` carries the new problems of a Repair round into the next run of
the Profile, and `ProfileResult` is what one run returns.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from prismtex.agent.context import ContextAssembler
from prismtex.agent.events import Event
from prismtex.agent.providers import ModelSpec
from prismtex.agent.staging import Staging
from prismtex.agent.steps import StepRunner
from prismtex.agent.threads import ThreadMemory
from prismtex.agent.tools import ToolBox
from prismtex.agent.types import Intent, TurnRequest
from prismtex.agent.verify import DiagnosticView
from prismtex.cancel import CancelToken
from prismtex.outline import Outline

__all__ = ["MAX_REPAIR_ROUNDS", "ProfileResult", "ProfileStop", "RepairFeedback", "TurnScope"]

MAX_REPAIR_ROUNDS: Final = 2  # Repair rounds per Turn (agent design §4.7)

ProfileStop = Literal["idle", "refused", "gave_up", "clarify"]


@dataclass(frozen=True)
class TurnScope:
    """Everything a Profile may touch during one Turn.

    `outline` returns the staged Outline (cached by digest); `diagnostics` are the
    last known Baseline Diagnostics and may be empty; `emit` sends an event to the
    Turn's Trace; `prime_baseline` starts the Baseline compile and is idempotent.
    """

    turn_id: str
    request: TurnRequest
    intent: Intent
    spec: ModelSpec
    steps: StepRunner
    staging: Staging
    outline: Callable[[], Outline]
    diagnostics: Sequence[DiagnosticView]
    context: ContextAssembler
    memory: ThreadMemory
    tools: ToolBox
    emit: Callable[[Event], object]
    prime_baseline: Callable[[], None]
    cancel: CancelToken


@dataclass(frozen=True)
class RepairFeedback:
    """The problems a Repair round asks the Profile to fix; `round` counts from 1."""

    round: int
    new: tuple[DiagnosticView, ...]
    host_fixes: tuple[str, ...]


@dataclass(frozen=True)
class ProfileResult:
    """What one run of a Profile produced.

    `reply` is the model's prose for the user. `stop` says why the run ended:
    "idle" (the model finished), "refused" (the Provider refused), "gave_up"
    (the repeat guard tripped) or "clarify" (the Profile asked where to edit).
    `edit_intent` is True when the request asked for a change, so a Turn that
    staged nothing ends as `no_change` rather than `answered`.
    """

    reply: str
    stop: ProfileStop
    edit_intent: bool
