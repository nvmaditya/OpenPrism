"""The OpenAI-compatible adapter (agent design §11.3).

`OpenAICompatModel` sends one `ModelCall` to a Chat Completions endpoint through the
official `openai` SDK and streams neutral events back. One preset describes the
endpoint (`catalog.PRESETS`): its base URL, whether it needs a key and is paid, which
output-cap parameter it takes and whether it accepts `parallel_tool_calls`.

- **Request.** `chat.completions.create(stream=True, stream_options={"include_usage":
  True})` with the output cap under the preset's `max_tokens_param`, always sent.
  `parallel_tool_calls=False` goes with tools where the preset accepts it. Sampling
  parameters are sent only when the spec lists them (no preset does). A router Step's
  response schema becomes a `json_schema` response format.
- **Stream.** `tool_calls[i].function.arguments` fragments are assembled by `index`; the
  id and name come from the first fragment that carries them. The calls are emitted
  when the choice finishes; on a `length` stop the last call is marked
  `parse_error="truncated"`. `finish_reason` maps `stop` to `end`, `tool_calls` to
  `tool_use`, `length` to `max_tokens` and `content_filter` to `refusal`. A reply with
  tool calls that ends normally is `tool_use` even when the server reports `stop`, and a
  reply that carries `refusal` text is a refusal. Reasoning deltas (`reasoning_content`,
  `reasoning`) announce thinking and are not kept.
- **Usage** comes from the final chunk: uncached input is `prompt_tokens -
  prompt_tokens_details.cached_tokens`, and the cached tokens are cache reads. A server
  that sends no usage gets no `UsageReport`, so the budget guard settles the Step at its
  full reservation.
- **History.** `Stopped.message.native` is the assistant message in Chat Completions
  form (content and tool calls with their exact argument strings) and is replayed
  unchanged. Host-built messages render from their blocks: tool results become `tool`
  messages, first, then any text as one user message.
- **Keys and base URLs.** The base URL and key are always passed to the SDK, so it reads
  neither `OPENAI_API_KEY` nor `OPENAI_BASE_URL`; what it reads from other `OPENAI_*`
  variables (organization, project, admin key, `OPENAI_CUSTOM_HEADERS`) is dropped. A
  preset with a fixed base URL refuses any other, so its key reaches only that host. A
  preset whose key is optional sends the literal "unused" when none is set. Errors never
  contain the key.
- **Ledger.** A paid model (a paid preset, or a model metered by a price) runs only
  against the one cost ledger, so the adapter cannot be built without its path (agent
  design §11.8). The budget guard writes the ledger; the adapter never does.
- **Retries** are the SDK's (`max_retries=2`), which happen only before the response
  starts, so output is never duplicated.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx
import openai

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

if TYPE_CHECKING:
    # Type-only: the catalog imports this module to build models.
    from prismtex.agent.providers.catalog import Preset
    from prismtex.agent.tools import ToolSpec

__all__ = ["MAX_RETRIES", "UNUSED_KEY", "OpenAICompatModel"]

# The SDK retries only before the first streamed byte, so output is never duplicated.
MAX_RETRIES: Final = 2
# Sent as the key by a preset whose key is optional and unset; the SDK requires one.
UNUSED_KEY: Final = "unused"

_KEY_SETTING = "PRISMTEX_OPENAI_API_KEY"
_URL_SETTING = "PRISMTEX_OPENAI_BASE_URL"
_FINISH: Mapping[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}
_CONTEXT_ERROR = re.compile(
    r"context[ _]length|context window|maximum context|too many tokens|prompt is too long"
    r"|reduce the length",
    re.IGNORECASE,
)
_REASON_LIMIT = 300


class OpenAICompatModel(ModelPort):
    """One model behind an OpenAI-compatible Chat Completions endpoint.

    `preset` must be the preset the spec's ref names. `base_url` must equal the preset's
    own base URL unless the preset lets `PRISMTEX_OPENAI_BASE_URL` set it. `api_key` is
    required by a preset with `key_required`. `ledger_path` is required when the model
    is paid; None then raises a `ValueError` naming `PRISMTEX_LEDGER_PATH`.
    """

    spec: ModelSpec

    def __init__(
        self,
        spec: ModelSpec,
        *,
        preset: Preset,
        base_url: str,
        api_key: str | None,
        http_client: httpx.AsyncClient,
        ledger_path: Path | None = None,
    ) -> None:
        if spec.provider != "openai_compatible":
            raise ValueError(f"{spec.ref} is not an OpenAI-compatible model")
        if spec.ref.partition(":")[0] != preset.name:
            raise ValueError(f"{spec.ref} is not a model of the {preset.name} preset")
        self.paid = spec.paid or preset.paid
        self._ledger_path = require_ledger(ledger_path) if self.paid else ledger_path
        url = _base_url(base_url)
        if not preset.base_url_configurable and url != preset.base_url:
            raise ValueError(
                f"the {preset.name} preset sends its key only to {preset.base_url}, not {url}"
            )
        if not api_key:
            if preset.key_required:
                raise ValueError(f"the {preset.name} preset needs an API key; set {_KEY_SETTING}")
            api_key = UNUSED_KEY
        self.spec = spec
        self.preset = preset
        self.base_url = url
        self._secret = api_key if api_key != UNUSED_KEY else None
        self._tool_ids = 0
        # Explicit key and base URL: the SDK reads neither from the environment.
        self._client = openai.AsyncOpenAI(
            api_key=api_key, base_url=url, http_client=http_client, max_retries=MAX_RETRIES
        )
        _drop_environment_settings(self._client)

    @property
    def ledger_path(self) -> Path | None:
        """The cost ledger every paid Step of this model is reserved and settled in."""
        return self._ledger_path

    def __repr__(self) -> str:
        return f"OpenAICompatModel({self.spec.ref!r}, base_url={self.base_url!r})"

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        params = self._params(call)
        try:
            chunks = await self._client.chat.completions.create(**params, stream=True)
        except (openai.APIError, httpx.HTTPError) as error:
            raise self._error(error) from None
        async with chunks:  # closes the HTTP stream however the iterator ends
            reply = _Reply(self.spec.model, self._next_tool_id)
            try:
                async for item in chunks:
                    for event in reply.feed(_chunk_data(item)):
                        yield event
            except (openai.APIError, httpx.HTTPError, ValueError) as error:
                raise self._error(error) from None
            for event in reply.finish():
                yield event

    async def count_tokens(self, call: ModelCall) -> int | None:
        return None  # Chat Completions has no counting endpoint; use the estimator

    def _next_tool_id(self) -> str:
        """An id for a tool call the server sent without one."""
        self._tool_ids += 1
        return f"call_{self._tool_ids}"

    # -- request -------------------------------------------------------------------

    def _params(self, call: ModelCall) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.spec.model,
            "messages": _messages(call),
            "stream_options": {"include_usage": True},
            self.preset.max_tokens_param: call.max_output_tokens,
        }
        if call.tools:
            params["tools"] = [_tool(tool) for tool in call.tools]
            if self.preset.parallel_tool_calls:
                params["parallel_tool_calls"] = False
        if call.response_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": _plain(call.response_schema)},
            }
        if call.seed is not None:
            params["seed"] = call.seed
        if self.spec.sampling:
            params["extra_body"] = dict(self.spec.sampling)
        return params

    # -- errors --------------------------------------------------------------------

    def _error(self, error: BaseException) -> ProviderError:
        code, message, retry_after, retryable = _classify(error, self.spec.model)
        if message is not None and self._secret:
            message = message.replace(self._secret, "[redacted]")
        return ProviderError(code, message, retry_after_s=retry_after, retryable=retryable)


def _base_url(base_url: str) -> str:
    """`base_url` without surrounding space or a trailing slash.

    Raises `ValueError` unless it is an http or https URL with a host.
    """
    url = base_url.strip().rstrip("/")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"{_URL_SETTING} must be an http or https URL")
    return url


def _drop_environment_settings(client: openai.AsyncOpenAI) -> None:
    """Forget what the SDK read from `OPENAI_*` variables meant for other tools.

    The constructor reads `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`,
    `OPENAI_WEBHOOK_SECRET` and `OPENAI_CUSTOM_HEADERS` when no value is passed, and
    would send the organization, the project and the custom headers to this endpoint.
    """
    client.organization = None
    client.project = None
    client.admin_api_key = None
    client.webhook_secret = None
    client._custom_headers = {}  # pyright: ignore[reportPrivateUsage]


# -- messages ----------------------------------------------------------------------


def _messages(call: ModelCall) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if call.system:
        messages.append({"role": "system", "content": call.system})
    for message in call.messages:
        if message.native is not None:
            messages.append(_plain(message.native))
        elif message.role == "assistant":
            messages.append(_assistant_json(message.blocks))
        else:
            messages.extend(_user_json(message.blocks))
    return messages


def _assistant_json(blocks: tuple[Block, ...]) -> dict[str, Any]:
    text = "\n\n".join(block.text for block in blocks if isinstance(block, TextBlock))
    calls = [
        {
            "id": block.id,
            "type": "function",
            "function": {"name": block.name, "arguments": _json(_plain(block.input))},
        }
        for block in blocks
        if isinstance(block, ToolUseBlock)
    ]
    return _assistant_message(text, calls)


def _assistant_message(text: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    # Content may be null only beside tool calls.
    message: dict[str, Any] = {"role": "assistant", "content": text or (None if calls else "")}
    if calls:
        message["tool_calls"] = calls
    return message


def _user_json(blocks: tuple[Block, ...]) -> list[dict[str, Any]]:
    """Tool results first, as `tool` messages right after the calls; then the text."""
    messages: list[dict[str, Any]] = []
    texts: list[str] = []
    for block in blocks:
        if isinstance(block, ToolResultBlock):
            messages.append(
                {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
            )
        elif isinstance(block, TextBlock):
            if block.text:
                texts.append(block.text)
        else:
            raise ValueError("a user message cannot carry a tool call")
    if texts:
        messages.append({"role": "user", "content": "\n\n".join(texts)})
    return messages


def _tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": _plain(tool.input_schema),
        },
    }


def _plain(value: Any) -> Any:
    """`value` as plain JSON data: mappings become dicts, sequences lists, in order."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# -- the streamed reply --------------------------------------------------------------


def _chunk_data(item: object) -> Mapping[str, Any]:
    """One stream chunk as the fields the server sent."""
    if isinstance(item, openai.BaseModel):
        return item.to_dict(warnings=False)
    if isinstance(item, Mapping):
        return item  # pyright: ignore[reportUnknownVariableType]
    raise ValueError("unreadable stream chunk")


@dataclass
class _Call:
    """One tool call being assembled from its fragments."""

    id: str | None = None
    name: str | None = None
    fragments: list[str] = field(default_factory=list[str])


class _Reply:
    """The state of one streamed reply, fed one chunk at a time.

    Only choice 0 is read; the request never asks for more.
    """

    def __init__(self, model: str, next_id: Callable[[], str]) -> None:
        self.model = model
        self._next_id = next_id
        self.text: list[str] = []
        self.calls: dict[int, _Call] = {}
        self.finish_reason: str | None = None
        self.usage: Mapping[str, Any] | None = None
        self.thinking = False
        self.refused = False
        self.emitted: list[ToolCallCompleted] = []

    def feed(self, data: Mapping[str, Any]) -> list[ModelEvent]:
        model = data.get("model")
        if isinstance(model, str) and model:
            self.model = model
        usage = data.get("usage")
        if isinstance(usage, Mapping):
            self.usage = usage  # pyright: ignore[reportUnknownMemberType]
        events: list[ModelEvent] = []
        choices = data.get("choices")
        for choice in choices if isinstance(choices, list) else ():  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(choice, Mapping) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(delta, Mapping):
                events.extend(self._delta(delta))  # pyright: ignore[reportUnknownArgumentType]
            finish = choice.get("finish_reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(finish, str) and finish and self.finish_reason is None:
                self.finish_reason = finish
                events.extend(self._complete_calls())
        return events

    def finish(self) -> list[ModelEvent]:
        """The usage and the `Stopped` event; raises when the reply never finished."""
        if self.finish_reason is None:
            raise ProviderError(
                "unreachable", "The provider closed the stream before the reply finished."
            )
        if self.refused and not self.emitted:
            reason: StopReason = "refusal"
        elif self.emitted and self.finish_reason in ("stop", "tool_calls", "function_call"):
            reason = "tool_use"
        else:
            reason = _FINISH.get(self.finish_reason, "end")
        events: list[ModelEvent] = []
        if self.usage is not None:
            events.append(UsageReport(_step_usage(self.usage, self.model)))
        events.append(Stopped(reason, self._message()))
        return events

    def _delta(self, delta: Mapping[str, Any]) -> list[ModelEvent]:
        events: list[ModelEvent] = []
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning and not self.thinking:
            self.thinking = True
            events.append(ThinkingStarted())
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.text.append(content)
            events.append(TextDelta(content))
        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            self.refused = True
        fragments = delta.get("tool_calls")
        for fragment in fragments if isinstance(fragments, list) else ():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(fragment, Mapping):
                self._fragment(fragment)  # pyright: ignore[reportUnknownArgumentType]
        return events

    def _fragment(self, fragment: Mapping[str, Any]) -> None:
        index = fragment.get("index")
        call_id = fragment.get("id")
        if not isinstance(index, int) or isinstance(index, bool):
            # A server that omits the index: a fragment with a new id starts a new call.
            last = max(self.calls, default=None)
            if last is None:
                index = 0
            elif call_id and call_id != self.calls[last].id:
                index = last + 1
            else:
                index = last
        call = self.calls.setdefault(index, _Call())
        if isinstance(call_id, str) and call_id and call.id is None:
            call.id = call_id
        function = fragment.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(name, str) and name and call.name is None:
                call.name = name
            arguments = function.get("arguments")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(arguments, str):
                call.fragments.append(arguments)

    def _complete_calls(self) -> list[ModelEvent]:
        order = sorted(self.calls)
        events: list[ModelEvent] = []
        for index in order:
            call = self.calls[index]
            raw = "".join(call.fragments) or "{}"
            truncated = self.finish_reason == "length" and index == order[-1]
            args = None if truncated else _object(raw)
            completed = ToolCallCompleted(
                id=call.id or self._next_id(),
                name=call.name or "",
                raw_args=raw,
                args=args,
                parse_error="truncated" if truncated else None,
                recovered=False,
            )
            self.emitted.append(completed)
            events.append(completed)
        return events

    def _message(self) -> Message:
        text = "".join(self.text)
        blocks: list[Block] = [TextBlock(text)] if text else []
        blocks.extend(ToolUseBlock(c.id, c.name, c.args or {}) for c in self.emitted)
        calls = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw_args}}
            for c in self.emitted
        ]
        return Message("assistant", tuple(blocks), native=_assistant_message(text, calls))


def _object(raw: str) -> dict[str, Any] | None:
    """The arguments as an object, read leniently so a single-escaped `\\section` survives."""
    parsed = parse_tool_arguments(raw, {})
    return dict(parsed.args) if parsed.args is not None else None


def _step_usage(usage: Mapping[str, Any], model: str | None) -> StepUsage:
    prompt = _count(usage.get("prompt_tokens"))
    output = _count(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, Mapping) else 0  # pyright: ignore[reportUnknownMemberType]
    cached = min(cached, prompt)
    return StepUsage(
        input_tokens=prompt - cached,
        output_tokens=output,
        cache_read_tokens=cached,
        served_model=model,
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# -- errors ----------------------------------------------------------------------------


def _classify(
    error: BaseException, model: str
) -> tuple[ProviderErrorCode, str | None, float | None, bool | None]:
    """The code, message (None: the default), retry-after and retryable of `error`."""
    if isinstance(error, openai.APITimeoutError | httpx.TimeoutException):
        return "timeout", None, None, None
    if isinstance(error, openai.APIStatusError):
        status, reason = error.status_code, _reason(error)
        code = str(error.code or "")
        if status in (401, 403):
            return "auth", None, None, None
        if status == 404:
            return (
                "not_found",
                f"The provider does not serve the model {model}. Check the model name and "
                f"{_URL_SETTING}.",
                None,
                None,
            )
        if status == 402 or code == "insufficient_quota":
            message = "The provider reports that the account is out of credit or quota."
            return "rate_limited", message, None, False
        if status == 429:
            return "rate_limited", None, _retry_after(error.response.headers), None
        if status in (408, 504):
            return "timeout", None, None, None
        if status == 413 or code == "context_length_exceeded" or _CONTEXT_ERROR.search(reason):
            return "context_overflow", None, None, None
        if status >= 500:
            return "overloaded", None, None, None
        if reason:
            return (
                "bad_request",
                f"The provider rejected the request: {_shorten(reason)}",
                None,
                None,
            )
        return "bad_request", None, None, None
    if isinstance(error, openai.APIConnectionError | httpx.HTTPError):
        return "unreachable", None, None, None
    if isinstance(error, openai.APIError):
        # An error event inside the stream.
        if _CONTEXT_ERROR.search(_reason(error)):
            return "context_overflow", None, None, None
        return "overloaded", "The provider failed while writing the reply. Try again.", None, None
    return (
        "bad_request",
        f"The provider sent a reply that could not be read. Check {_URL_SETTING}.",
        None,
        False,
    )


def _reason(error: openai.APIError) -> str:
    """The server's own error message, or "".

    The SDK passes the `error` member of an error body, or the whole body when it has
    none (vLLM sends `{"object": "error", "message": ...}`).
    """
    body = error.body
    if isinstance(body, str):
        return body
    message = body.get("message") if isinstance(body, Mapping) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(message, str):
        return message
    if isinstance(error, openai.APIStatusError):
        return ""  # the SDK's own message repeats the raw body
    return error.message


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
