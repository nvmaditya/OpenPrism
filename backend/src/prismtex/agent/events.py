"""Turn events, v1 (agent design §9): the envelope, the event union and its JSON Schema.

Every event travels as one JSON object, `{"v": 1, "seq", "turn_id", "at", "type",
...fields}`, and streams as one SSE frame. Each public type of §9.2 is a frozen
dataclass named `<CamelType>Event`, with its wire name in the class variable
`type`. Fields marked optional in §9.2 default to None and are left out of the
JSON while None; every other field is always present.

Internal records (`model_call`, `tool_io`, `decision`) are written to the Trace
and never streamed (§2.2). `event_schema()` covers the 17 public types only.
New event types and new optional fields keep `v` at 1; renaming or removing
anything, or changing a meaning, bumps it (§9.1).
"""

from __future__ import annotations

import dataclasses
import json
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import (
    Any,
    ClassVar,
    Final,
    Literal,
    TypedDict,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from prismtex.agent.types import Intent, Outcome, ProfileName, StaleHunk, TurnStatus, Usage, Verdict

EVENT_VERSION: Final = 1

# String values of `tool_call.args` are cut to this many characters.
TOOL_ARG_PREVIEW_CHARS: Final = 200
# `tool_io` keeps at most this many UTF-8 bytes of arguments and of results.
TOOL_IO_MAX_BYTES: Final = 4096

StatusState = Literal["preparing", "locating", "working", "verifying", "repairing", "finalizing"]
RegionKind = Literal[
    "selection",
    "previous",
    "paragraph",
    "environment",
    "section",
    "diagnostic",
    "insertion",
    "file",
]
StopReason = Literal["end", "tool_use", "max_tokens", "refusal"]
Severity = Literal["error", "warning", "info"]

# §9.3 codes.
ErrorCode = Literal[
    "provider_auth",
    "provider_not_found",
    "provider_rate_limited",
    "provider_overloaded",
    "provider_unreachable",
    "provider_timeout",
    "provider_bad_request",
    "provider_refused",
    "history_rejected",
    "context_overflow",
    "budget_exhausted",
    "interrupted",
    "internal",
]
NoticeCode = Literal[
    "editor_context_dropped",
    "region_split",
    "continued_later",
    "prompt_trimmed",
    "proposal_carried_partially",
    "malformed_reply",
    "text_tool_call_recovered",
    "history_reset",
    "unknown_model",
    "compile_unavailable",
    "budget_near",
    "auto_accept_skipped",
    "previous_turn_stopped",
]
ToolErrorCode = Literal[
    "unknown_tool",
    "invalid_args",
    "read_only_turn",
    "escape_damage",
    "control_char",
    "path_denied",
    "write_denied",
    "not_found",
    "ambiguous",
    "exists",
    "binary",
    "too_large",
    "stale_view",
    "out_of_range",
    "repeated",
    "check_limit",
    "truncated_args",
    "cancelled",
]


# -- JSON objects nested in events ---------------------------------------------


class TurnLimitsJSON(TypedDict):
    """`turn_started.limits`; `max_cost_usd` is None for a free Provider."""

    max_steps: int
    max_wall_s: float
    max_cost_usd: float | None


class DiagnosticJSON(TypedDict):
    """One Diagnostic in `diagnostics.new` and `.fixed`: a compile Diagnostic plus its line."""

    file: str | None
    line: int | None
    severity: Severity
    kind: str
    message: str
    excerpt: str | None


class ResolvedFileJSON(TypedDict):
    """A file `resolved` wrote; `version` is None for a deleted file."""

    path: str
    version: str | None


class StepUsageJSON(TypedDict):
    """The tokens one Step reported."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    served_model: str


class RemainingJSON(TypedDict):
    """What is left of the Turn's limits; `cost_usd` is None without a cost limit."""

    steps: int
    tokens: int
    cost_usd: float | None
    wall_s: float


# -- the 17 public types (§9.2) ------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TurnStartedEvent:
    """Always seq 1."""

    type: ClassVar[str] = "turn_started"
    thread_id: str
    profile: ProfileName
    model: str
    intent: Intent
    limits: TurnLimitsJSON


@dataclass(frozen=True, kw_only=True)
class StatusEvent:
    """Every state change; `detail` reads like "Repair round 1 of 2"."""

    type: ClassVar[str] = "status"
    state: StatusState
    detail: str | None = None


@dataclass(frozen=True, kw_only=True)
class RegionEvent:
    """Guided Profile, before that Region's Step; lines are 1-based and inclusive."""

    type: ClassVar[str] = "region"
    index: int
    path: str
    start_line: int
    end_line: int
    kind: RegionKind
    label: str


@dataclass(frozen=True, kw_only=True)
class MessageStartedEvent:
    """Before the first token of an assistant message."""

    type: ClassVar[str] = "message_started"
    message_id: str
    step: int


@dataclass(frozen=True, kw_only=True)
class TokenEvent:
    """A prose delta of the current message."""

    type: ClassVar[str] = "token"
    message_id: str
    delta: str


@dataclass(frozen=True, kw_only=True)
class MessageCompletedEvent:
    """The end of an assistant message."""

    type: ClassVar[str] = "message_completed"
    message_id: str
    text: str
    stop: StopReason


@dataclass(frozen=True, kw_only=True)
class RewriteDeltaEvent:
    """Guided replacement text as it is written: a preview, not a Proposal."""

    type: ClassVar[str] = "rewrite_delta"
    index: int
    delta: str


@dataclass(frozen=True, kw_only=True)
class ToolCallEvent:
    """A tool call whose arguments are complete.

    String values in `args`, at any depth, are cut to 200 characters; the
    Trace's `tool_io` record keeps more.
    """

    type: ClassVar[str] = "tool_call"
    call_id: str
    message_id: str
    name: str
    args: Mapping[str, Any]
    recovered: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _cut_strings(self.args, TOOL_ARG_PREVIEW_CHARS))


@dataclass(frozen=True, kw_only=True)
class ToolResultEvent:
    """Exactly one per `tool_call`; `changed` lists the staged paths."""

    type: ClassVar[str] = "tool_result"
    call_id: str
    ok: bool
    summary: str
    error_code: ToolErrorCode | None = None
    changed: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class CompileEvent:
    """Around each compile; a cache hit sends only `cached`."""

    type: ClassVar[str] = "compile"
    stage: Literal["baseline", "proposal"]
    round: int
    state: Literal["started", "finished", "cached"]
    ok: bool | None = None
    errors: int | None = None
    warnings: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, kw_only=True)
class DiagnosticsEvent:
    """After each Proposal compile."""

    type: ClassVar[str] = "diagnostics"
    round: int
    new: tuple[DiagnosticJSON, ...]
    fixed: tuple[DiagnosticJSON, ...]
    persisting_errors: int
    host_fixes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class ProposalEvent:
    """At most once, in finalization, after the final verification.

    `proposal` is the Proposal view of agent design §10.3: the Proposal
    without `new_text`, with `truncated` set when it was cut at 256 KiB.
    """

    type: ClassVar[str] = "proposal"
    proposal: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class ResolvedEvent:
    """Auto-accept only."""

    type: ClassVar[str] = "resolved"
    proposal_id: str
    applied: tuple[str, ...]
    stale: tuple[StaleHunk, ...]
    files: tuple[ResolvedFileJSON, ...]
    checkpoint_id: str | None = None
    by: Literal["auto"]


@dataclass(frozen=True, kw_only=True)
class UsageEvent:
    """After each Step, and once in finalization.

    `step_usage` is None when there is no reported Step usage to show, as
    for a Step settled at its reservation.
    """

    type: ClassVar[str] = "usage"
    step: int
    step_usage: StepUsageJSON | None
    totals: Usage
    remaining: RemainingJSON


@dataclass(frozen=True, kw_only=True)
class NoticeEvent:
    """Something worth showing that does not end the Turn."""

    type: ClassVar[str] = "notice"
    code: NoticeCode
    message: str


@dataclass(frozen=True, kw_only=True)
class ErrorEvent:
    """Something the user must know; a fatal error is followed by finalization and `done`."""

    type: ClassVar[str] = "error"
    code: ErrorCode
    message: str
    retryable: bool
    fatal: bool
    detail: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class DoneEvent:
    """Exactly once, last."""

    type: ClassVar[str] = "done"
    status: TurnStatus
    outcome: Outcome
    verdict: Verdict
    summary: str
    reply: str
    proposal_id: str | None = None
    usage: Usage
    error_code: ErrorCode | None = None


# -- internal records: written to the Trace, never streamed (§2.2) -------------


@dataclass(frozen=True, kw_only=True)
class ModelCallRecord:
    """One model call.

    `input_tokens` and the token counts after it are what the Provider
    reported (None when it reported nothing). `parts` is the estimated
    token count of each context part. `stop` is None when the call failed.
    """

    type: ClassVar[str] = "model_call"
    step: int
    kind: Literal["reply", "draft", "route"]
    model: str
    prompt_version: str
    estimated_input_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    latency_ms: int
    stop: StopReason | None
    parts: Mapping[str, int]


@dataclass(frozen=True, kw_only=True)
class ToolIORecord:
    """A tool call's raw arguments and the result the model saw, each cut at 4 KiB."""

    type: ClassVar[str] = "tool_io"
    call_id: str
    name: str
    args: str
    result: str
    ok: bool
    error_code: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _cut_utf8(self.args, TOOL_IO_MAX_BYTES))
        object.__setattr__(self, "result", _cut_utf8(self.result, TOOL_IO_MAX_BYTES))


@dataclass(frozen=True, kw_only=True)
class DecisionRecord:
    """A choice the host made.

    `kind` names what was decided: the Profile, the intent, a locate rule, a
    trim, a guard trip, a host repair, a segment rollover.
    """

    type: ClassVar[str] = "decision"
    kind: str
    choice: str
    detail: Mapping[str, Any] = field(default_factory=dict[str, Any])


PublicEvent = (
    TurnStartedEvent
    | StatusEvent
    | RegionEvent
    | MessageStartedEvent
    | TokenEvent
    | MessageCompletedEvent
    | RewriteDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | CompileEvent
    | DiagnosticsEvent
    | ProposalEvent
    | ResolvedEvent
    | UsageEvent
    | NoticeEvent
    | ErrorEvent
    | DoneEvent
)
InternalRecord = ModelCallRecord | ToolIORecord | DecisionRecord
Event = PublicEvent | InternalRecord

PUBLIC_EVENTS: Final[tuple[type[PublicEvent], ...]] = get_args(PublicEvent)
INTERNAL_TYPES: Final = frozenset(cls.type for cls in get_args(InternalRecord))
ENVELOPE_KEYS: Final = ("v", "seq", "turn_id", "at", "type")


# -- the envelope (§9.1) -------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """One event as written to the Trace and sent to clients.

    `data` holds the event's fields as JSON values. An internal envelope is
    marked `"internal": true` in its JSON and never streams.
    """

    v: int
    seq: int
    turn_id: str
    at: str
    type: str
    data: Mapping[str, Any]
    internal: bool = False

    @classmethod
    def of(
        cls, event: Event, *, seq: int, turn_id: str, at: str, internal: bool = False
    ) -> Envelope:
        """Wrap an event; internal records are always internal."""
        hidden = internal or event.type in INTERNAL_TYPES
        return cls(EVENT_VERSION, seq, turn_id, at, event.type, _event_data(event), hidden)

    @classmethod
    def from_json(cls, line: str | bytes) -> Envelope:
        """Parse one line of a Trace; raises ValueError for anything else."""
        body = json.loads(line)
        if not isinstance(body, dict):
            raise ValueError("an envelope is a JSON object")
        data: dict[str, Any] = dict(body)
        v, seq, turn_id, at, type_ = (data.pop(key, None) for key in ENVELOPE_KEYS)
        internal = data.pop("internal", False)
        valid = (
            isinstance(v, int)
            and isinstance(seq, int)
            and isinstance(turn_id, str)
            and isinstance(at, str)
            and isinstance(type_, str)
            and isinstance(internal, bool)
        )
        if not valid:
            raise ValueError("not a Turn event envelope")
        return cls(v, seq, turn_id, at, type_, data, internal)

    def to_json(self) -> str:
        """Compact JSON: the envelope keys first, then the event's fields."""
        body: dict[str, Any] = {
            "v": self.v,
            "seq": self.seq,
            "turn_id": self.turn_id,
            "at": self.at,
            "type": self.type,
        }
        if self.internal:
            body["internal"] = True
        body.update(self.data)
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def to_sse(self) -> str:
        """One SSE frame; `seq` is the event id."""
        if self.internal:
            raise ValueError("internal envelopes are never streamed")
        return f"id: {self.seq}\nevent: {self.type}\ndata: {self.to_json()}\n\n"


def _event_data(event: Event) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for f in dataclasses.fields(event):
        value = getattr(event, f.name)
        if value is None and f.default is None:
            continue  # an optional field that is not set
        data[f.name] = _jsonable(value)
    return data


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, frozenset | set):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _cut_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        return {str(k): _cut_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_cut_strings(v, limit) for v in value]
    return value


def _cut_utf8(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


# -- JSON Schema of the public types -------------------------------------------


def event_schema() -> dict[str, Any]:
    """The JSON Schema (2020-12) of the 17 public event types, envelope included.

    Each event type and each nested object is one entry of `$defs`, named
    after its class (without the `JSON` suffix of the nested TypedDicts).
    """
    defs: dict[str, Any] = {}
    for cls in PUBLIC_EVENTS:
        defs[cls.__name__] = _event_def(cls, defs)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PrismTeX Turn events",
        "description": (
            f"Turn events, v{EVENT_VERSION}. Clients ignore unknown event types and fields."
        ),
        "oneOf": [{"$ref": f"#/$defs/{cls.__name__}"} for cls in PUBLIC_EVENTS],
        "$defs": dict(sorted(defs.items())),
    }


def _event_def(cls: type[PublicEvent], defs: dict[str, Any]) -> dict[str, Any]:
    hints = get_type_hints(cls)
    properties: dict[str, Any] = {
        "v": {"type": "integer", "const": EVENT_VERSION},
        "seq": {"type": "integer", "minimum": 1},
        "turn_id": {"type": "string"},
        "at": {"type": "string", "format": "date-time"},
        "type": {"type": "string", "const": cls.type},
    }
    required: list[str] = list(ENVELOPE_KEYS)
    for f in dataclasses.fields(cls):
        optional = f.default is None
        properties[f.name] = _schema(hints[f.name], defs, drop_null=optional)
        if not optional:
            required.append(f.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_SCALARS: Final[dict[Any, str]] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


def _schema(tp: Any, defs: dict[str, Any], *, drop_null: bool = False) -> dict[str, Any]:
    if tp is Any:
        return {}
    if tp in _SCALARS:
        return {"type": _SCALARS[tp]}
    origin = get_origin(tp)
    args = get_args(tp)
    if origin is Literal:
        return {"type": "string", "enum": list(args)}
    if origin is Union or origin is types.UnionType:
        members = [a for a in args if not (drop_null and a is type(None))]
        if len(members) == 1:
            return _schema(members[0], defs)
        return {"anyOf": [_schema(a, defs) for a in members]}
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        return {"type": "array", "items": _schema(args[0], defs)}
    if origin is Mapping or origin is dict:
        if args[1] is Any:
            return {"type": "object"}
        return {"type": "object", "additionalProperties": _schema(args[1], defs)}
    if is_typeddict(tp) or (dataclasses.is_dataclass(tp) and isinstance(tp, type)):
        return _ref(tp, defs)
    raise TypeError(f"no JSON Schema for {tp!r}")


def _ref(cls: type, defs: dict[str, Any]) -> dict[str, Any]:
    name = cls.__name__.removesuffix("JSON")  # DiagnosticJSON is "Diagnostic" in the schema
    if name not in defs:
        defs[name] = {}  # placeholder, so a cycle ends here
        hints = get_type_hints(cls)
        if is_typeddict(cls):
            names = list(hints)
            required = [n for n in names if n in cls.__required_keys__]
        else:
            names = [f.name for f in dataclasses.fields(cls)]
            required = names
        defs[name] = {
            "type": "object",
            "properties": {n: _schema(hints[n], defs) for n in names},
            "required": required,
            "additionalProperties": False,
        }
    return {"$ref": f"#/$defs/{name}"}
