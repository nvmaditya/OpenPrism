"""Value types of the agent's public surface (agent design §2.1).

Records are frozen dataclasses. Lines are 1-based; columns count code points in
the LF text of the editor's base. A version is a content version: the SHA-256
hex digest of a file's bytes on disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

ProfileName = Literal["agentic", "guided"]
Intent = Literal["question", "edit", "unknown"]
TurnStatus = Literal["completed", "cancelled", "failed", "budget_exhausted", "interrupted"]
Outcome = Literal["answered", "proposed", "applied", "no_change", "clarify", "nothing"]
Verdict = Literal["clean", "regressed", "unverified", "no_change"]
# The Turn state machine of agent design §8.1; written to the manifest on every transition.
TurnState = Literal[
    "accepted", "preparing", "locating", "working", "verifying", "repairing", "finalizing", "done"
]
StaleReason = Literal["version_mismatch", "context_changed", "file_missing", "file_exists"]


@dataclass(frozen=True)
class Selection:
    """A span of the editor's base text; `end_col` is exclusive.

    `(n, 1)` as the end means "up to the start of line n".
    """

    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass(frozen=True)
class EditorContext:
    """What the editor shows for one file when the user sends a message.

    `version` is the content version the buffer was loaded from or last saved
    as. `buffer` is the unsaved LF text, present if and only if the buffer is
    dirty (at most 2 MiB).
    """

    path: str
    version: str
    cursor_line: int | None = None
    selection: Selection | None = None
    buffer: str | None = None


@dataclass(frozen=True)
class TurnRequest:
    """A request to start a Turn.

    `message` is 1 character to 64 KiB, newlines kept. `thread_id` None starts
    a new Thread. `model` is "preset:model"; None means the Project setting,
    then the default model.
    """

    project_id: str
    message: str
    thread_id: str | None = None
    editor: EditorContext | None = None
    model: str | None = None
    profile: ProfileName | None = None


@dataclass(frozen=True)
class ResolveRequest:
    """Accept or reject Hunks of an open Proposal.

    `base_versions` is an optional If-Match per path: a file whose current
    version differs is not written.
    """

    project_id: str
    proposal_id: str
    accept: Literal["all"] | frozenset[str] = frozenset()
    reject: Literal["all"] | frozenset[str] = frozenset()
    base_versions: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Usage:
    """What a Turn consumed.

    `input_tokens` counts every prompt token, cached or not. `cost_usd` comes
    from integer micro-USD. `estimated` is True when some Step was settled at
    its reservation.
    """

    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    estimated: bool = False


@dataclass(frozen=True)
class ErrorInfo:
    """Why a Turn failed, in plain language: no secrets and no host paths."""

    code: str
    message: str
    retryable: bool


class ProposalRef(Protocol):
    """The part of a Proposal (agent design §6.1) that these value types rely on."""

    @property
    def id(self) -> str: ...


@dataclass(frozen=True)
class TurnOutcome:
    """How a Turn ended and what it produced.

    `status` is how it ended, `outcome` what it produced; `verdict` is
    "no_change" without a Proposal. `summary` is the host-written outcome
    line; `reply` is the model's prose, always shown as the model's.
    """

    turn_id: str
    thread_id: str
    status: TurnStatus
    outcome: Outcome
    verdict: Verdict
    summary: str
    reply: str
    proposal: ProposalRef | None
    usage: Usage
    error: ErrorInfo | None


@dataclass(frozen=True)
class StaleHunk:
    """A Hunk `resolve` did not apply; it stays undecided."""

    hunk_id: str
    path: str
    reason: StaleReason


@dataclass(frozen=True)
class FileState:
    """A file after `resolve`: `version` and `text` are None when it was deleted.

    `text` lets the editor refresh buffers that have no unsaved changes.
    """

    path: str
    version: str | None
    text: str | None


@dataclass(frozen=True)
class ResolveResult:
    """What `resolve` did. `closed` is True when no undecided Hunks remain."""

    proposal_id: str
    applied: tuple[str, ...]
    rejected: tuple[str, ...]
    stale: tuple[StaleHunk, ...]
    undecided: tuple[str, ...]
    files: tuple[FileState, ...]
    checkpoint_id: str | None
    closed: bool
