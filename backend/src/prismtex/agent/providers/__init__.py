"""The Provider port (agent design §2.2 `providers`, §11.1).

An adapter turns one neutral `ModelCall` into one Provider request and streams neutral
`ModelEvent`s back: text, thinking and tool-call events, then exactly one `Stopped`, or
it raises `ProviderError`. Closing the iterator cancels the request. A `ModelResolver`
picks the adapter for a model ref. `catalog.spec(ref)` gives a model's capabilities.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    # Type-only: budget and tools import this package for their own records.
    from prismtex.agent.budget import Price
    from prismtex.agent.tools import ToolSpec

__all__ = [
    "Block",
    "HealthIssue",
    "HealthReport",
    "Memory",
    "Message",
    "ModelCall",
    "ModelEvent",
    "ModelInfo",
    "ModelPort",
    "ModelResolver",
    "ModelSpec",
    "ModelUnavailable",
    "ProfileName",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderHealth",
    "ProviderName",
    "ProviderNotice",
    "StepUsage",
    "StopReason",
    "Stopped",
    "TextBlock",
    "TextDelta",
    "ThinkingStarted",
    "ToolCall",
    "ToolCallCompleted",
    "ToolResultBlock",
    "ToolUseBlock",
    "UnavailableCode",
    "UsageReport",
]

ProfileName = Literal["agentic", "guided"]
ProviderName = Literal["anthropic", "openai_compatible", "ollama", "fake"]
Memory = Literal["transcript", "records"]
StopReason = Literal["end", "tool_use", "max_tokens", "refusal"]
ProviderErrorCode = Literal[
    "auth",
    "not_found",
    "rate_limited",
    "overloaded",
    "unreachable",
    "timeout",
    "bad_request",
    "context_overflow",
    "history_rejected",
]
UnavailableCode = Literal["model_unavailable", "unknown_model", "profile_unfit"]


# -- requests ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a conversation.

    `blocks` is the neutral view. `native` is the exact Provider payload, which the same
    adapter replays verbatim (thinking blocks and signatures included); None for
    messages the host built.
    """

    role: Literal["user", "assistant"]
    blocks: tuple[Block, ...]
    native: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One model request.

    `max_output_tokens` is always sent. `response_schema` is for router Steps only and
    never carries LaTeX. `seed` is set for eval runs.
    """

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 1024
    response_schema: Mapping[str, Any] | None = None
    seed: int | None = None


# -- events --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingStarted:
    pass


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """A tool call whose arguments are complete.

    `raw_args` is the exact argument string received (Ollama: the arguments re-serialized
    as canonical JSON); the ToolBox always parses it. `args` is informational.
    `parse_error` is "truncated" for a call left open by a `max_tokens` stop.
    `recovered` marks a call recovered from text (agent design §4.6).
    """

    id: str
    name: str
    raw_args: str
    args: Mapping[str, Any] | None
    parse_error: str | None
    recovered: bool


# The neutral tool call handed to the ToolBox.
ToolCall = ToolCallCompleted


@dataclass(frozen=True, slots=True)
class StepUsage:
    """Tokens of one Step as the Provider reported them.

    `input_tokens` counts prompt tokens neither read from nor written to the cache.
    `iterations` holds one entry per served model when the Provider reports usage per
    model (Anthropic `usage.iterations`), so each part is priced by its served model.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    served_model: str | None = None
    iterations: tuple[StepUsage, ...] = ()


@dataclass(frozen=True, slots=True)
class UsageReport:
    usage: StepUsage


@dataclass(frozen=True, slots=True)
class Stopped:
    """The end of a Step: why it stopped and the assistant message it produced."""

    reason: StopReason
    message: Message
    refusal_category: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderNotice:
    """Something the adapter did that the user should hear about, such as a history reset."""

    code: Literal["history_reset"]
    message: str


ModelEvent = (
    TextDelta | ThinkingStarted | ToolCallCompleted | UsageReport | Stopped | ProviderNotice
)


# -- errors --------------------------------------------------------------------

_ERROR_MESSAGES: Mapping[str, str] = {
    "auth": "The provider rejected the API key.",
    "not_found": "The provider does not serve this model.",
    "rate_limited": "The provider is rate limiting requests. Try again shortly.",
    "overloaded": "The provider is overloaded. Try again shortly.",
    "unreachable": "The provider could not be reached.",
    "timeout": "The provider took too long to respond.",
    "bad_request": "The provider rejected the request.",
    "context_overflow": "The request does not fit in the model's context window.",
    "history_rejected": "The provider rejected the conversation history.",
}
_RETRYABLE = frozenset({"rate_limited", "overloaded", "unreachable", "timeout", "history_rejected"})


class ProviderError(Exception):
    """A Provider request failed.

    `message` is plain language with no secrets and no host paths. `retryable` defaults
    by code (agent design §9.3); `retry_after_s` comes from a `retry-after` header.
    """

    code: ProviderErrorCode
    retryable: bool
    retry_after_s: float | None
    message: str

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str | None = None,
        *,
        retryable: bool | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError(f"unknown provider error code: {code!r}")
        self.code = code
        self.message = message or _ERROR_MESSAGES[code]
        self.retryable = code in _RETRYABLE if retryable is None else retryable
        self.retry_after_s = retry_after_s
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"ProviderError({self.code!r}, {self.message!r}, retryable={self.retryable}, "
            f"retry_after_s={self.retry_after_s})"
        )


_UNAVAILABLE_MESSAGES: Mapping[str, str] = {
    "model_unavailable": "The model is not available.",
    "unknown_model": "Unknown model.",
    "profile_unfit": "This model cannot run that Profile.",
}


class ModelUnavailable(Exception):
    """A model ref cannot be served; `env_var` names the setting that would fix it."""

    code: UnavailableCode
    env_var: str | None
    message: str

    def __init__(
        self, code: UnavailableCode, message: str | None = None, *, env_var: str | None = None
    ) -> None:
        if code not in _UNAVAILABLE_MESSAGES:
            raise ValueError(f"unknown model-unavailable code: {code!r}")
        self.code = code
        self.message = message or _UNAVAILABLE_MESSAGES[code]
        self.env_var = env_var
        super().__init__(self.message)


# -- capabilities --------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSpec:
    """What the engine needs to know about one model (agent design §11.6).

    - `ref` is "preset:model"; `model` is the Provider's model id; `label` is for people.
    - `context_window` is the window served (for Ollama the pinned `num_ctx`);
      `input_cap` bounds the prompt below it (None: from the window).
    - `max_output` is the output cap of an agentic Step; guided Steps set their own
      (agent design §4.7).
    - `sampling` is sent as given and is empty where nothing may be sent (Anthropic).
    - `prompt_caching`: the adapter places cache breakpoints (Anthropic).
    - `quirks`: "recover_text_tool_calls" (agent design §4.6); "unknown_model", a model
      that is usable but not a supported default, so a Turn on it gets a notice.
    - `paid` with `price` None means the price is unknown; such a model is refused.
    """

    ref: str
    provider: ProviderName
    model: str
    label: str
    context_window: int
    input_cap: int | None
    max_output: int
    native_tools: bool
    memory: Memory
    prompt_caching: bool
    default_profile: ProfileName
    sampling: Mapping[str, float]
    effort: str | None
    num_ctx: int | None
    chars_per_token: float = 3.0
    price: Price | None
    paid: bool
    quirks: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A model as `GET /api/agent/models` lists it (agent design §10.3)."""

    ref: str
    provider: ProviderName
    model: str
    label: str
    default_profile: ProfileName
    context_window: int
    input_cap: int | None
    max_output: int
    native_tools: bool
    paid: bool
    price: Price | None
    available: bool

    @classmethod
    def from_spec(cls, spec: ModelSpec, *, available: bool) -> ModelInfo:
        return cls(
            ref=spec.ref,
            provider=spec.provider,
            model=spec.model,
            label=spec.label,
            default_profile=spec.default_profile,
            context_window=spec.context_window,
            input_cap=spec.input_cap,
            max_output=spec.max_output,
            native_tools=spec.native_tools,
            paid=spec.paid,
            price=spec.price,
            available=available,
        )


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    configured: bool
    reachable: bool | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class HealthIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The agent's configuration as `GET /api/agent/health` reports it (agent design §10.3)."""

    ok: bool
    default_model: str | None
    providers: tuple[ProviderHealth, ...]
    issues: tuple[HealthIssue, ...]


# -- ports ---------------------------------------------------------------------


class ModelPort(Protocol):
    """One model behind one Provider."""

    spec: ModelSpec

    def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        """Send `call` and stream its events; closing the iterator cancels the request."""
        ...

    async def count_tokens(self, call: ModelCall) -> int | None:
        """The exact prompt size, or None when the Provider cannot count (use the estimator)."""
        ...


class ModelResolver(Protocol):
    """Picks the model for a Turn."""

    def resolve(self, ref: str | None, project_default: str | None) -> ModelPort:
        """The model for `ref`, else the Project's default; raises `ModelUnavailable`."""
        ...

    def catalog(self) -> Sequence[ModelInfo]:
        """Every model this resolver can serve; makes no network request."""
        ...

    async def health(self, *, probe: bool = False) -> HealthReport:
        """The configuration; `probe` may contact local runtimes, never a paid endpoint."""
        ...
