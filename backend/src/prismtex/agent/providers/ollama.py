"""The native Ollama adapter (agent design §11.4).

`OllamaModel` streams `POST {base_url}/api/chat` as NDJSON. The OpenAI-compatible `/v1`
endpoint is never used, because it ignores `num_ctx`. Every request carries the same
pinned `num_ctx`, since a change reloads the model, and keeps the model loaded for 30
minutes. `tools` is sent only when the call has tools (agentic Steps) and `format` only
when it has a response schema (router Steps). Sampling comes from the catalog and `seed`
from the call.

Tool calls arrive whole in `message.tool_calls`; their arguments are re-serialized as
canonical JSON into `raw_args`. Usage is `prompt_eval_count` and `eval_count`, priced at
zero. As a backstop to the client-side budget, a reply whose counts reach
`num_ctx - 32` fails with `context_overflow`, because the server may have shifted the
context. A low `prompt_eval_count` is normal: Ollama does not count a reused KV prefix.

A failed request is tried once more after a short delay, but only before the first byte
of the reply arrives, so output is never duplicated. Timeouts are the client's.

`list_models` (`GET /api/tags`) and `show_capabilities` (`POST /api/show`) serve the
health probe and models the catalog does not know.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx

from prismtex.agent.providers import (
    Block,
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ModelSpec,
    ProviderError,
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

if TYPE_CHECKING:
    from prismtex.agent.tools import ToolSpec

__all__ = [
    "KEEP_ALIVE",
    "OVERFLOW_MARGIN",
    "OllamaModel",
    "list_models",
    "show_capabilities",
]

KEEP_ALIVE = "30m"
# A reply whose prompt and output tokens come this close to `num_ctx` filled the window.
OVERFLOW_MARGIN = 32

_URL_SETTING = "PRISMTEX_OLLAMA_URL"
_NUM_CTX_SETTING = "PRISMTEX_OLLAMA_NUM_CTX"
_HEADERS = {"content-type": "application/json", "accept": "application/x-ndjson"}
_CONTEXT_ERROR = re.compile(
    r"context (?:length|size|window)|max(?:imum)? context|num_ctx|prompt (?:is )?too long",
    re.IGNORECASE,
)
_NO_TOOLS_ERROR = re.compile(r"does not support tools", re.IGNORECASE)
_DETAIL_LIMIT = 2_000  # characters of an error body read to classify it; never shown


class OllamaModel(ModelPort):
    """One model served by Ollama's native chat API.

    `num_ctx` is sent on every request. When it differs from the spec's window, `spec`
    is the given spec with `context_window` and `num_ctx` set to it, so the budget and
    the server agree on the window. `retry_delay_s` is the pause before the second
    attempt.
    """

    def __init__(
        self,
        spec: ModelSpec,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        num_ctx: int,
        retry_delay_s: float = 0.5,
    ) -> None:
        if spec.provider != "ollama":
            raise ValueError(f"OllamaModel serves ollama models, not {spec.ref!r}")
        if num_ctx <= OVERFLOW_MARGIN:
            raise ValueError(f"num_ctx must be more than {OVERFLOW_MARGIN} tokens")
        if retry_delay_s < 0:
            raise ValueError("retry_delay_s must not be negative")
        if spec.num_ctx != num_ctx or spec.context_window != num_ctx:
            spec = replace(spec, num_ctx=num_ctx, context_window=num_ctx)
        self.spec = spec
        self._url = _endpoint(base_url, "/api/chat")
        self._client = client
        self._num_ctx = num_ctx
        self._retry_delay_s = retry_delay_s
        self._tool_ids = 0

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        content = _encode(self._body(call))
        response, chunks, first = await self._open(content)
        reply = _Reply(self.spec.model, self._num_ctx, self._next_tool_id)
        lines = _Lines()
        try:
            chunk: bytes | None = first
            while chunk is not None:
                for line in lines.push(chunk):
                    for event in reply.feed(line):
                        yield event
                    if reply.finished:
                        return
                chunk = await _next_chunk(chunks)
            for event in reply.feed(lines.rest()):
                yield event
            if not reply.finished:
                raise _connection_lost()
        finally:
            await response.aclose()  # closes the HTTP stream under `chunks`

    async def count_tokens(self, call: ModelCall) -> int | None:
        return None  # Ollama cannot count a prompt without running it; use the estimator

    # -- request -----------------------------------------------------------------

    def _body(self, call: ModelCall) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.spec.model,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "messages": _messages(call),
        }
        if call.tools:
            body["tools"] = [_tool_json(tool) for tool in call.tools]
        if call.response_schema is not None:
            body["format"] = _plain(call.response_schema)
        options: dict[str, Any] = {"num_ctx": self._num_ctx, "num_predict": call.max_output_tokens}
        for name, value in self.spec.sampling.items():
            options.setdefault(name, value)
        if call.seed is not None:
            options["seed"] = call.seed
        body["options"] = options
        return body

    async def _open(self, content: bytes) -> tuple[httpx.Response, AsyncIterator[bytes], bytes]:
        """The response and its first bytes; one retry, before any byte of the reply."""
        try:
            return await self._attempt(content)
        except ProviderError as error:
            if not error.retryable:
                raise
        await asyncio.sleep(self._retry_delay_s)
        return await self._attempt(content)

    async def _attempt(self, content: bytes) -> tuple[httpx.Response, AsyncIterator[bytes], bytes]:
        request = self._client.build_request("POST", self._url, content=content, headers=_HEADERS)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TransportError as error:
            raise _transport_error(error) from error
        try:
            if response.status_code != 200:
                await _read_quietly(response)
                raise _status_error(response, self.spec.model)
            chunks = response.aiter_bytes()
            first = b""
            while not first:
                chunk = await _next_chunk(chunks)
                if chunk is None:
                    raise _connection_lost()
                first = chunk
            return response, chunks, first
        except BaseException:
            await response.aclose()
            raise

    def _next_tool_id(self) -> str:
        self._tool_ids += 1
        return f"call_{self._tool_ids}"


async def list_models(base_url: str, client: httpx.AsyncClient) -> set[str]:
    """The names of the models Ollama has pulled (`GET /api/tags`).

    A name tagged `:latest` is listed with and without the tag, since Ollama treats
    `llama3.1` and `llama3.1:latest` as the same model.
    """
    data = await _request_json(client, "GET", _endpoint(base_url, "/api/tags"), None, model=None)
    models = data.get("models")
    if models is None:
        return set()
    if not isinstance(models, list):
        raise _unreadable()
    names: set[str] = set()
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if isinstance(name, str) and name:
            names.add(name)
            if name.endswith(":latest"):
                names.add(name.removesuffix(":latest"))
    return names


async def show_capabilities(base_url: str, model: str, client: httpx.AsyncClient) -> frozenset[str]:
    """What Ollama says `model` can do (`POST /api/show`), such as "completion" and "tools".

    An Ollama too old to report capabilities gives the empty set.
    """
    url = _endpoint(base_url, "/api/show")
    data = await _request_json(client, "POST", url, {"model": model}, model=model)
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list):
        return frozenset()
    return frozenset(item for item in capabilities if isinstance(item, str))


# -- the streamed reply ------------------------------------------------------------


class _Reply:
    """The state of one streamed reply, fed one NDJSON line at a time."""

    def __init__(self, model: str, num_ctx: int, next_tool_id: Callable[[], str]) -> None:
        self._model = model
        self._num_ctx = num_ctx
        self._next_tool_id = next_tool_id
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._native_calls: list[Any] = []
        self._calls: list[ToolCallCompleted] = []
        self.finished = False

    def feed(self, line: bytes) -> Iterator[ModelEvent]:
        if self.finished or not line.strip():
            return
        try:
            data = json.loads(line)
        except ValueError:
            raise _unreadable() from None
        if not isinstance(data, dict):
            raise _unreadable()
        if "error" in data:
            raise _stream_error(data["error"])
        message = data.get("message")
        if message is not None:
            yield from self._message_part(message)
        if data.get("done") is True:
            self.finished = True
            yield from self._finish(data)

    def _message_part(self, message: Any) -> Iterator[ModelEvent]:
        if not isinstance(message, dict):
            raise _unreadable()
        thinking = message.get("thinking")
        if isinstance(thinking, str) and thinking:
            if not self._thinking:
                yield ThinkingStarted()
            self._thinking.append(thinking)
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise _unreadable()
        if content:
            self._text.append(content)
            yield TextDelta(content)
        tool_calls = message.get("tool_calls")
        if tool_calls is None:
            return
        if not isinstance(tool_calls, list):
            raise _unreadable()
        for native in tool_calls:
            call = self._tool_call(native)
            self._native_calls.append(native)
            self._calls.append(call)
            yield call

    def _tool_call(self, native: Any) -> ToolCallCompleted:
        if not isinstance(native, dict):
            raise _unreadable()
        function = native.get("function")
        if not isinstance(function, dict):
            raise _unreadable()
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise _unreadable()
        arguments = function.get("arguments")
        args: Mapping[str, Any] | None
        if arguments is None or isinstance(arguments, dict):
            args = arguments or {}
            raw_args = _canonical(args)
        elif isinstance(arguments, str):
            raw_args, args = arguments, _object_or_none(arguments)
        else:
            raise _unreadable()
        call_id = native.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = self._next_tool_id()
        return ToolCallCompleted(
            id=call_id, name=name, raw_args=raw_args, args=args, parse_error=None, recovered=False
        )

    def _finish(self, data: Mapping[str, Any]) -> Iterator[ModelEvent]:
        prompt = _count(data, "prompt_eval_count")
        output = _count(data, "eval_count")
        served = data.get("model")
        yield UsageReport(
            StepUsage(prompt, output, served_model=served if isinstance(served, str) else None)
        )
        if prompt + output >= self._num_ctx - OVERFLOW_MARGIN:
            raise ProviderError(
                "context_overflow",
                f"The reply filled the model's context window ({prompt + output} of "
                f"{self._num_ctx} tokens), so Ollama may have cut the prompt. Shorten the "
                f"request, or raise {_NUM_CTX_SETTING}.",
            )
        reason: StopReason
        if data.get("done_reason") == "length":
            reason = "max_tokens"
        elif self._calls:
            reason = "tool_use"
        else:
            reason = "end"
        yield Stopped(reason, self._assistant_message())

    def _assistant_message(self) -> Message:
        text = "".join(self._text)
        blocks: list[Block] = [TextBlock(text)] if text else []
        blocks.extend(ToolUseBlock(call.id, call.name, call.args or {}) for call in self._calls)
        native: dict[str, Any] = {"role": "assistant", "content": text}
        if self._thinking:
            native["thinking"] = "".join(self._thinking)
        if self._native_calls:
            native["tool_calls"] = self._native_calls
        return Message("assistant", tuple(blocks), native)


class _Lines:
    """Splits a byte stream into lines."""

    def __init__(self) -> None:
        self._buffer = b""

    def push(self, chunk: bytes) -> list[bytes]:
        *lines, self._buffer = (self._buffer + chunk).split(b"\n")
        return lines

    def rest(self) -> bytes:
        rest, self._buffer = self._buffer, b""
        return rest


async def _next_chunk(chunks: AsyncIterator[bytes]) -> bytes | None:
    """The next chunk of the body, or None at its end."""
    try:
        return await anext(chunks)
    except StopAsyncIteration:
        return None
    except httpx.TransportError as error:
        raise _transport_error(error) from error


def _count(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key, 0)  # Ollama omits a count of zero
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _unreadable()
    return value


# -- messages ----------------------------------------------------------------------


def _messages(call: ModelCall) -> list[dict[str, Any]]:
    """The Ollama messages for `call`.

    An assistant message this adapter produced is replayed from its `native` payload as
    received. A tool result becomes a `tool` message named after its call; the text
    blocks between results become one user message.
    """
    messages: list[dict[str, Any]] = []
    if call.system:
        messages.append({"role": "system", "content": call.system})
    tool_names: dict[str, str] = {}
    for message in call.messages:
        if message.role == "assistant":
            for block in message.blocks:
                if isinstance(block, ToolUseBlock):
                    tool_names[block.id] = block.name
            if message.native is not None:
                messages.append(_plain(message.native))
            else:
                messages.append(_assistant_json(message.blocks))
        else:
            messages.extend(_user_json(message.blocks, tool_names))
    return messages


def _assistant_json(blocks: tuple[Block, ...]) -> dict[str, Any]:
    text = "\n\n".join(block.text for block in blocks if isinstance(block, TextBlock))
    calls = [
        {"function": {"name": block.name, "arguments": _plain(block.input)}}
        for block in blocks
        if isinstance(block, ToolUseBlock)
    ]
    result: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        result["tool_calls"] = calls
    return result


def _user_json(blocks: tuple[Block, ...], tool_names: Mapping[str, str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    texts: list[str] = []

    def flush() -> None:
        if texts:
            messages.append({"role": "user", "content": "\n\n".join(texts)})
            texts.clear()

    for block in blocks:
        if isinstance(block, TextBlock):
            texts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            flush()
            result: dict[str, Any] = {"role": "tool", "content": block.content}
            name = tool_names.get(block.tool_use_id)
            if name is not None:
                result["tool_name"] = name
            messages.append(result)
        else:
            raise ValueError("a user message cannot carry a tool call")
    flush()
    return messages


def _tool_json(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": _plain(tool.input_schema),
        },
    }


def _plain(value: Any) -> Any:
    """`value` with every mapping as a dict and every list or tuple as a list."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _encode(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _canonical(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _object_or_none(raw: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# -- other endpoints -----------------------------------------------------------------


def _endpoint(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    try:
        url = httpx.URL(base)
    except httpx.InvalidURL:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.host:
        raise ValueError(f"{_URL_SETTING} must be an http or https URL")
    return base + path


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    payload: Mapping[str, Any] | None,
    *,
    model: str | None,
) -> dict[str, Any]:
    content = None if payload is None else _encode(payload)
    headers = {"content-type": "application/json"} if content is not None else None
    try:
        response = await client.request(method, url, content=content, headers=headers)
    except httpx.TransportError as error:
        raise _transport_error(error) from error
    if response.status_code != 200:
        raise _status_error(response, model)
    try:
        data = response.json()
    except ValueError:
        raise _unreadable() from None
    if not isinstance(data, dict):
        raise _unreadable()
    return data


async def _read_quietly(response: httpx.Response) -> None:
    """Read an error body for classification; a failure leaves it empty."""
    try:
        await response.aread()
    except httpx.HTTPError:
        pass


# -- errors --------------------------------------------------------------------------


def _transport_error(error: httpx.TransportError) -> ProviderError:
    if isinstance(error, httpx.TimeoutException):
        return ProviderError(
            "timeout",
            "Ollama took too long to respond. Loading a model can take a while; try again.",
        )
    if isinstance(error, httpx.ConnectError):
        return ProviderError(
            "unreachable", f"Could not connect to Ollama. Start Ollama, or set {_URL_SETTING}."
        )
    return _connection_lost()


def _connection_lost() -> ProviderError:
    return ProviderError(
        "unreachable", "The connection to Ollama closed before the reply was complete."
    )


def _unreadable() -> ProviderError:
    return ProviderError(
        "bad_request",
        f"Ollama sent a reply that could not be read. Check that {_URL_SETTING} points to Ollama.",
        retryable=False,
    )


def _status_error(response: httpx.Response, model: str | None) -> ProviderError:
    """The error for a response that is not 200; the body is read only to classify it."""
    status = response.status_code
    detail = _detail(response)
    if status in (401, 403):
        return ProviderError(
            "auth",
            f"The Ollama server refused access (HTTP {status}). Check {_URL_SETTING} and any "
            "proxy in front of Ollama.",
        )
    if status == 404:
        if model is None:
            return ProviderError(
                "not_found", f"No Ollama API answered at {_URL_SETTING}. Check the URL."
            )
        return ProviderError("not_found", f"Ollama does not have {model}. Run: ollama pull {model}")
    if _CONTEXT_ERROR.search(detail):
        return ProviderError(
            "context_overflow",
            "The request does not fit in the model's context window. Shorten the request, "
            f"or raise {_NUM_CTX_SETTING}.",
        )
    if status == 429:
        return ProviderError(
            "rate_limited",
            "Ollama is limiting requests. Try again shortly.",
            retry_after_s=_retry_after(response),
        )
    if status in (408, 504):
        return ProviderError("timeout", "Ollama took too long to respond.")
    if status >= 500:
        return ProviderError(
            "overloaded",
            f"Ollama could not serve the request (HTTP {status}). Try again shortly; if it "
            "keeps failing, check the Ollama server log.",
        )
    if model is not None and _NO_TOOLS_ERROR.search(detail):
        return ProviderError("bad_request", f"{model} does not support tool calls in Ollama.")
    return ProviderError("bad_request", f"Ollama rejected the request (HTTP {status}).")


def _stream_error(error: Any) -> ProviderError:
    """The error for an `{"error": ...}` line inside a streamed reply."""
    if isinstance(error, str) and _CONTEXT_ERROR.search(error):
        return ProviderError(
            "context_overflow",
            "The request does not fit in the model's context window. Shorten the request, "
            f"or raise {_NUM_CTX_SETTING}.",
        )
    return ProviderError(
        "overloaded",
        "Ollama failed while writing the reply. Try again; if it keeps failing, check the "
        "Ollama server log.",
    )


def _detail(response: httpx.Response) -> str:
    try:
        body = response.content
    except httpx.ResponseNotRead:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return body[:_DETAIL_LIMIT].decode("utf-8", "replace")
    error = data.get("error") if isinstance(data, dict) else None
    return error[:_DETAIL_LIMIT] if isinstance(error, str) else ""


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None
