"""Errors raised by the agent's entry points (agent design §2.1).

They mean "can't start", "can't find" or "can't resolve". Whatever happens
during a Turn is reported through events and the TurnOutcome instead. The
HTTP layer maps `code` to a status and puts `detail` (JSON-safe) beside
`code` and `message` in the error envelope.
"""

from __future__ import annotations

from typing import Literal

RejectCode = Literal[
    "busy",
    "project_not_found",
    "thread_not_found",
    "invalid_request",
    "editor_out_of_date",
    "model_unavailable",
    "budget_exhausted",
]


class AgentError(Exception):
    """Base class: a stable `code`, a plain-language `message` and JSON-safe `detail`."""

    code: str
    message: str
    detail: dict[str, object]

    def __init__(self, code: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail)


class TurnRejected(AgentError):
    """`submit` cannot start the Turn; `code` says why."""

    def __init__(self, code: RejectCode, message: str, **detail: object) -> None:
        super().__init__(code, message, **detail)


class TurnNotFound(AgentError):
    def __init__(self, turn_id: str) -> None:
        super().__init__("turn_not_found", "This turn does not exist.", turn_id=turn_id)


class ProposalNotFound(AgentError):
    def __init__(self, proposal_id: str) -> None:
        super().__init__(
            "proposal_not_found", "This proposal does not exist.", proposal_id=proposal_id
        )


class ProposalClosed(AgentError):
    """The Proposal was resolved or superseded; `open_proposal_id` names the open one."""

    def __init__(self, proposal_id: str, open_proposal_id: str | None) -> None:
        super().__init__(
            "proposal_closed",
            "This proposal is closed: it was resolved or replaced by a newer one.",
            proposal_id=proposal_id,
            open_proposal_id=open_proposal_id,
        )


class ProjectBusy(AgentError):
    """A Turn is active in the Project; `active_turn_id` names it."""

    def __init__(self, active_turn_id: str) -> None:
        super().__init__(
            "project_busy",
            "The agent is working on this project. Wait for it to finish, or stop it.",
            active_turn_id=active_turn_id,
        )
