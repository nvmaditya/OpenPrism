"""The Anthropic adapter (agent design §11.2).

`AnthropicModel` sends one `ModelCall` to the Messages API through the official SDK and
streams neutral events back.

- **Request.** `max_tokens` is always sent; `temperature`, `top_p`, `top_k` and
  `tool_choice` never are. Thinking is adaptive and the effort is explicit. The system
  text carries a cache breakpoint and the request enables top-level automatic caching.
  Tools are sorted by name and sent strict, with eager input streaming.
- **Stream.** The adapter reads the raw event stream (`messages.create(stream=True)`)
  and accumulates `input_json_delta` fragments itself, so tool arguments arrive exactly
  as the model wrote them, even when they are not valid JSON (a single-escaped
  `\\section`); the ToolBox parses them. A tool call is emitted once its block has
  closed and the next block starts or the reply ends; the last block of a `max_tokens`
  stop, closed or not, is emitted with `parse_error="truncated"`.
- **Verbatim history.** `Stopped.message.native` is the assistant message with its
  content blocks as received, thinking blocks and signatures included, and is replayed
  unchanged. Messages the host built render from their neutral blocks, every tool
  result first.
- **History check.** With `strict_history` the request asks the API to fail on any
  edited history (beta `thinking-binding-controls-2026-08-01`). Without it, a history
  whose thinking blocks are bound to a different conversation is retried once with every
  thinking block removed, announced by `ProviderNotice("history_reset")`.
- **Keys.** The key is passed in explicitly and only ever sent to the SDK's default base
  URL; the SDK reads neither `ANTHROPIC_API_KEY` nor `ANTHROPIC_BASE_URL`. Error
  messages never contain it.
- **Ledger.** A paid model runs only against the one cost ledger, so the adapter cannot
  be built without its path (agent design §11.8). The budget guard reserves and settles
  every Step in that ledger; the adapter never writes it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import anthropic
import httpx

from prismtex.agent.budget import require_ledger
from prismtex.agent.providers import (
    Block,
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ModelSpec,
    ProviderError,
    ProviderErrorCode,
    ProviderNotice,
    StepUsage,
    Stopped,
    StopReason,
    TextBlock,
    TextDelta,
    ThinkingStarted,
    ToolCallCompleted,
    ToolResultBlock,
    ToolUseBlock,
    UsageReport,
)
from prismtex.agent.providers.toolcalls import parse_tool_arguments
from prismtex.agent.tools import ToolSpec

__all__ = [
    "BASE_URL",
    "BINDING_BETA",
    "EFFORTS",
    "FALLBACK_BETA",
    "FALLBACK_MODELS",
    "MAX_RETRIES",
    "AnthropicModel",
]

BASE_URL: Final = "https://api.anthropic.com"
# The SDK retries only before the first streamed byte, so output is never duplicated.
MAX_RETRIES: Final = 2
EFFORTS: Final = frozenset({"low", "medium", "high", "xhigh", "max"})
BINDING_BETA: Final = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
# Server-side refusal fallback exists for these models only; Haiku 5.5 has none.
FALLBACK_MODELS: Final = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
# The 400 the API returns when a replayed thinking block no longer matches its history.
_BOUND_HISTORY = "bound to a different conversation"
_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "pause_turn": "end",  # not expected: no server tools are used
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
}
_RESET_NOTICE = (
    "The provider no longer accepted the earlier thinking in this conversation, so this "
    "Step was sent again without it."
)
_REASON_LIMIT = 300


class AnthropicModel(ModelPort):
    """Claude Haiku, Sonnet or Opus 5.5 through the Anthropic Messages API.

    `ledger_path` is the one cost ledger; None raises a `ValueError` naming
    `PRISMTEX_LEDGER_PATH`, so no paid call runs unmetered. `effort` is sent as
    `output_config.effort` and stays fixed for the model's lifetime (a change would
    invalidate the cached conversation). `fallbacks` enables server-side refusal
    fallback for Opus 5.5 and Sonnet 5.5 and is ignored for Haiku.
    """

    spec: ModelSpec

    def __init__(
        self,
        spec: ModelSpec,
        *,
        api_key: str,
        http_client: httpx.AsyncClient,
        ledger_path: Path | None,
        effort: str = "medium",
        strict_history: bool = False,
        fallbacks: bool = False,
    ) -> None:
        self._ledger_path = require_ledger(ledger_path)
        if spec.provider != "anthropic":
            raise ValueError(f"{spec.ref} is not an anthropic model")
        if effort not in EFFORTS:
            raise ValueError(f"unknown effort {effort!r}; use one of {', '.join(sorted(EFFORTS))}")
        if not api_key:
            raise ValueError("an Anthropic API key is required")
        self.spec = spec
        self.effort = effort
        self.strict_history = strict_history
        self.fallbacks = fallbacks and spec.model in FALLBACK_MODELS
        self._secret = api_key
        # Explicit key and base URL: the SDK reads neither from the environment.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=BASE_URL, http_client=http_client, max_retries=MAX_RETRIES
        )

    @property
    def ledger_path(self) -> Path:
        """The cost ledger every paid Step of this model is reserved and settled in."""
        return self._ledger_path

    def __repr__(self) -> str:
        return (
            f"AnthropicModel({self.spec.ref!r}, effort={self.effort!r}, "
            f"strict_history={self.strict_history}, fallbacks={self.fallbacks})"
        )

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        messages = [_render(message) for message in call.messages]
        notice: ProviderNotice | None = None
        try:
            events = await self._open(call, messages)
        except _BoundHistory:
            if self.strict_history:
                raise ProviderError(
                    "history_rejected",
                    "The provider rejected the conversation history: an earlier message "
                    "changed after it was sent.",
                ) from None
            try:
                events = await self._open(call, _without_thinking(messages))
            except _BoundHistory:
                raise ProviderError(
                    "history_rejected",
                    "The provider rejected the conversation history, even without the "
                    "earlier thinking.",
                ) from None
            notice = ProviderNotice("history_reset", _RESET_NOTICE)
        async with events:  # closes the HTTP stream however the iterator ends
            if notice is not None:
                yield notice
            step = _Step(self.spec.model)
            try:
                async for event in events:
                    for neutral in step.feed(event.to_dict(warnings=False)):
                        yield neutral
            except (anthropic.APIError, httpx.HTTPError, json.JSONDecodeError) as error:
                raise self._error(error) from None
            for neutral in step.finish():
                yield neutral

    async def count_tokens(self, call: ModelCall) -> int | None:
        """The prompt size from the free `messages/count_tokens` endpoint.

        None when the history holds thinking blocks the API no longer accepts; the next
        `stream` resets it. Other failures raise `ProviderError`.
        """
        params = self._params(call, [_render(message) for message in call.messages], count=True)
        try:
            counted = await self._client.messages.count_tokens(**params)
        except anthropic.BadRequestError as error:
            if _is_bound_history(error):
                return None
            raise self._error(error) from None
        except (anthropic.APIError, httpx.HTTPError) as error:
            raise self._error(error) from None
        return counted.input_tokens

    # -- requests ------------------------------------------------------------------

    def _params(
        self, call: ModelCall, messages: list[dict[str, Any]], *, count: bool = False
    ) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": self.effort}
        if call.response_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": _plain(call.response_schema),
            }
        params: dict[str, Any] = {
            "model": self.spec.model,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "cache_control": {"type": "ephemeral"},
        }
        if not count:
            params["max_tokens"] = call.max_output_tokens
        if call.system:
            params["system"] = [
                {"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}
            ]
        if call.tools:
            params["tools"] = [_tool(spec) for spec in sorted(call.tools, key=lambda t: t.name)]
        return params

    def _betas(self) -> list[str]:
        betas: list[str] = []
        if self.strict_history:
            betas.append(BINDING_BETA)
        if self.fallbacks:
            betas.append(FALLBACK_BETA)
        return betas

    async def _open(
        self, call: ModelCall, messages: list[dict[str, Any]]
    ) -> anthropic.AsyncStream[Any]:
        """Send the request and return the event stream once the response has begun."""
        params = self._params(call, messages)
        betas = self._betas()
        try:
            if not betas:
                return await self._client.messages.create(**params, stream=True)
            extra: dict[str, Any] = {}
            if self.strict_history:
                # The installed SDK does not type `block_binding`, so the whole thinking
                # object goes through `extra_body`.
                params.pop("thinking")
                extra["thinking"] = {
                    "type": "adaptive",
                    "block_binding": {"prefix_mismatch_behavior": "error"},
                }
            if self.fallbacks:
                params["fallbacks"] = "default"
            return await self._client.beta.messages.create(
                **params, betas=betas, stream=True, extra_body=extra or None
            )
        except anthropic.BadRequestError as error:
            if _is_bound_history(error):
                raise _BoundHistory from None
            raise self._error(error) from None
        except (anthropic.APIError, httpx.HTTPError) as error:
            raise self._error(error) from None

    # -- errors --------------------------------------------------------------------

    def _error(self, error: BaseException) -> ProviderError:
        code, message, retry_after = _classify(error, self.spec.model)
        if message is not None:
            message = message.replace(self._secret, "[redacted]")
        return ProviderError(code, message, retry_after_s=retry_after)


class _BoundHistory(Exception):
    """The API refused thinking blocks that are bound to a different conversation."""


# -- rendering -------------------------------------------------------------------


def _tool(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": _plain(spec.input_schema),
        "strict": True,
        "eager_input_streaming": True,
    }


def _render(message: Message) -> dict[str, Any]:
    """The wire form of one message: its native payload, or its neutral blocks."""
    if message.native is not None:
        return _plain(message.native)
    blocks: Sequence[Block] = message.blocks
    if message.role == "user":
        # Every tool result of a Step first, then any text such as a STOP hint.
        blocks = sorted(blocks, key=lambda block: not isinstance(block, ToolResultBlock))
    content = [rendered for rendered in map(_block, blocks) if rendered is not None]
    return {"role": message.role, "content": content}


def _block(block: Block) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text} if block.text else None
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": _plain(block.input),
        }
    rendered: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
    }
    if block.is_error:
        rendered["is_error"] = True
    return rendered


def _without_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`messages` with every thinking block removed; emptied assistant messages go too."""
    kept: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            blocks = [block for block in content if _block_type(block) not in _THINKING_TYPES]
            if not blocks:
                continue  # consecutive user messages are combined by the API
            message = {**message, "content": blocks}
        kept.append(message)
    return kept


def _block_type(block: object) -> object:
    return block.get("type") if isinstance(block, dict) else None


def _plain(value: Any) -> Any:
    """`value` as plain JSON data: mappings become dicts, sequences lists, in order."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


# -- the streamed reply ----------------------------------------------------------


class _Step:
    """One streamed reply: native content, tool arguments, usage and the stop reason.

    A closed tool block is held until the next block starts or the reply ends, so the
    last block of a `max_tokens` stop can be marked truncated.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.content: dict[int, dict[str, Any]] = {}
        self.fragments: dict[int, list[str]] = {}
        self.open: set[int] = set()
        self.held: int | None = None
        self.usage: dict[str, Any] = {}
        self.stop_reason: str | None = None
        self.stop_details: Mapping[str, Any] | None = None
        self.finished = False

    def feed(self, event: Mapping[str, Any]) -> list[ModelEvent]:
        kind = event.get("type")
        if kind == "message_start":
            message = _mapping(event.get("message"))
            self.model = str(message.get("model") or self.model)
            self.usage.update(_present(message.get("usage")))
        elif kind == "content_block_start":
            released = self._release()
            index = int(event["index"])
            block = dict(_mapping(event.get("content_block")))
            self.content[index] = block
            if block.get("type") in _THINKING_TYPES:
                return [*released, ThinkingStarted()]
            if block.get("type") == "tool_use":
                self.fragments[index] = []
                self.open.add(index)
            return released
        elif kind == "content_block_delta":
            return self._delta(int(event["index"]), _mapping(event.get("delta")))
        elif kind == "content_block_stop":
            index = int(event["index"])
            if index in self.open:
                self.open.discard(index)
                released = self._release()
                self.held = index
                return released
        elif kind == "message_delta":
            delta = _mapping(event.get("delta"))
            self.stop_reason = delta.get("stop_reason") or self.stop_reason
            details = delta.get("stop_details")
            if isinstance(details, Mapping):
                self.stop_details = details
            self.usage.update(_present(event.get("usage")))
        elif kind == "message_stop":
            self.finished = True
        return []

    def finish(self) -> list[ModelEvent]:
        """The remaining tool calls, the usage and the `Stopped` event."""
        if not self.finished:
            raise ProviderError(
                "unreachable", "The provider closed the stream before the reply finished."
            )
        reason = _STOP_REASONS.get(self.stop_reason or "", "end")
        last = max(self.content, default=None)
        events: list[ModelEvent] = []
        pending = sorted(self.open | ({self.held} if self.held is not None else set()))
        for index in pending:
            cut = index in self.open or (reason == "max_tokens" and index == last)
            events.append(self._call(index, truncated=cut))
        self.open.clear()
        self.held = None
        content = [self.content[index] for index in sorted(self.content)]
        refusal = None
        if reason == "refusal" and self.stop_details is not None:
            category = self.stop_details.get("category")
            refusal = str(category) if category else None
        events.append(UsageReport(_step_usage(self.usage, self.model)))
        message = Message(
            "assistant", _neutral(content), native={"role": "assistant", "content": content}
        )
        events.append(Stopped(reason, message, refusal))
        return events

    def _delta(self, index: int, delta: Mapping[str, Any]) -> list[ModelEvent]:
        block = self.content.get(index)
        if block is None:
            return []
        kind = delta.get("type")
        if kind == "text_delta":
            text = str(delta.get("text") or "")
            block["text"] = str(block.get("text") or "") + text
            return [TextDelta(text)] if text else []
        if kind == "input_json_delta":
            self.fragments.setdefault(index, []).append(str(delta.get("partial_json") or ""))
        elif kind == "thinking_delta":
            block["thinking"] = str(block.get("thinking") or "") + str(delta.get("thinking") or "")
        elif kind == "signature_delta":
            block["signature"] = delta.get("signature")
        elif kind == "citations_delta":
            block["citations"] = [*(block.get("citations") or ()), delta.get("citation")]
        return []

    def _release(self) -> list[ModelEvent]:
        if self.held is None:
            return []
        index, self.held = self.held, None
        return [self._call(index, truncated=False)]

    def _call(self, index: int, *, truncated: bool) -> ToolCallCompleted:
        """The tool call of block `index`; its native `input` becomes the parsed object."""
        block = self.content[index]
        raw = "".join(self.fragments.get(index, ()))
        if not raw:
            start = block.get("input")
            raw = json.dumps(start if isinstance(start, Mapping) else {}, ensure_ascii=False)
        args = None if truncated else _object(raw)
        block["input"] = dict(args) if args is not None else {}
        return ToolCallCompleted(
            id=str(block.get("id") or ""),
            name=str(block.get("name") or ""),
            raw_args=raw,
            args=args,
            parse_error="truncated" if truncated else None,
            recovered=False,
        )


def _object(raw: str) -> dict[str, Any] | None:
    """The arguments as an object, read leniently so a single-escaped `\\section` survives."""
    parsed = parse_tool_arguments(raw, {})
    return dict(parsed.args) if parsed.args is not None else None


def _neutral(content: list[dict[str, Any]]) -> tuple[Block, ...]:
    blocks: list[Block] = []
    for block in content:
        if block.get("type") == "text" and block.get("text"):
            blocks.append(TextBlock(str(block["text"])))
        elif block.get("type") == "tool_use":
            blocks.append(
                ToolUseBlock(
                    str(block.get("id") or ""),
                    str(block.get("name") or ""),
                    dict(_mapping(block.get("input"))),
                )
            )
    return tuple(blocks)


def _step_usage(usage: Mapping[str, Any], model: str | None) -> StepUsage:
    """Anthropic usage as a `StepUsage`, with one entry per `usage.iterations` item."""
    items = usage.get("iterations")
    iterations = tuple(
        _step_usage(item, str(item["model"]) if item.get("model") else None)
        for item in (items if isinstance(items, list) else ())
        if isinstance(item, Mapping)
    )
    return StepUsage(
        input_tokens=_count(usage, "input_tokens"),
        output_tokens=_count(usage, "output_tokens"),
        cache_read_tokens=_count(usage, "cache_read_input_tokens"),
        cache_write_tokens=_count(usage, "cache_creation_input_tokens"),
        served_model=model,
        iterations=iterations,
    )


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _present(value: object) -> dict[str, Any]:
    """The fields of a usage object that are set; later events update earlier ones."""
    return {str(k): v for k, v in _mapping(value).items() if v is not None}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# -- errors ----------------------------------------------------------------------


def _is_bound_history(error: anthropic.APIStatusError) -> bool:
    return _BOUND_HISTORY in _reason(error.body).lower()


def _classify(
    error: BaseException, model: str
) -> tuple[ProviderErrorCode, str | None, float | None]:
    """The `ProviderError` code, message (None: the default) and retry-after of `error`."""
    if isinstance(error, anthropic.APITimeoutError | httpx.TimeoutException):
        return "timeout", None, None
    if isinstance(error, anthropic.APIStatusError):
        status, kind, reason = error.status_code, str(error.type or ""), _reason(error.body)
        if status in (401, 403) or kind in ("authentication_error", "permission_error"):
            return "auth", None, None
        if status == 404 or kind == "not_found_error":
            return "not_found", f"The provider does not serve the model {model}.", None
        if status == 429 or kind == "rate_limit_error":
            return "rate_limited", None, _retry_after(error.response.headers)
        if status == 504 or kind == "timeout_error":
            return "timeout", None, None
        if status == 413 or kind == "request_too_large" or "prompt is too long" in reason.lower():
            return "context_overflow", None, None
        if status >= 500 or kind in ("overloaded_error", "api_error"):
            return "overloaded", None, None
        if reason:
            return "bad_request", f"The provider rejected the request: {_shorten(reason)}", None
        return "bad_request", None, None
    if isinstance(error, anthropic.APIConnectionError | httpx.HTTPError):
        return "unreachable", None, None
    if isinstance(error, json.JSONDecodeError):
        return "unreachable", "The provider sent a malformed event stream.", None
    return "bad_request", "The provider sent a response that could not be read.", None


def _reason(body: object) -> str:
    """The API's own error message from an error body, or ""."""
    error = body.get("error") if isinstance(body, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else None
    return message if isinstance(message, str) else ""


def _shorten(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 3] + "..."


def _retry_after(headers: httpx.Headers) -> float | None:
    """Seconds from `retry-after-ms` or `retry-after` (in seconds), as the SDK reads them."""
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return max(0.0, float(value) / scale)
        except ValueError:
            continue
    return None
