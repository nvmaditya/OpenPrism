"""The offline contract every Provider adapter passes (agent design §11.1, §13.4).

Subclass `ModelPortContract` once per adapter in a test module, for example
`class TestOllamaContract(ModelPortContract)`, and fill in the hooks; pytest then runs
every `test_*` method below against that adapter through `httpx.MockTransport`. Import it
as `from tests.agent.providers.contract import ModelPortContract`.

Hooks:
- `make_port(handler)`: the adapter over `httpx.AsyncClient(transport=MockTransport(handler))`,
  with `secret` as its API key where it takes one, and the spec whose sampling it tests.
- `ok_response()`: a complete streamed reply: the text "Hi", one `view` tool call with the
  arguments {"path": "main.tex"}, and usage of 10 uncached input, 5 output and 3 cache-read
  tokens (or `expected_usage`).
- `text_prefix()`: the opening bytes of `ok_response()`, up to and including the text "Hi"
  and nothing after it.
- `error_response(kind)`: the response (or the exception the transport raises) for each of
  `ERROR_KINDS`. Keep any `retry-after` at 0, so SDK retries do not sleep.
- `max_output_path`: the keys under which the request body carries `max_output_tokens`,
  such as ("max_tokens",) or ("options", "num_predict").
- `forbidden_fields`: keys that must appear nowhere in a request body.
- `ok_raw_args`: the exact argument string `ok_response()` carries.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, ClassVar

import anyio
import httpx
import pytest

from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ProviderError,
    StepUsage,
    Stopped,
    TextBlock,
    TextDelta,
    ToolCallCompleted,
    UsageReport,
)
from prismtex.agent.tools import ToolSpec

__all__ = ["ERROR_KINDS", "RETRYABLE", "VIEW", "Handler", "ModelPortContract"]

Handler = Callable[[httpx.Request], httpx.Response]

ERROR_KINDS = ("auth", "not_found", "rate_limited", "overloaded", "unreachable", "timeout")
RETRYABLE = frozenset({"rate_limited", "overloaded", "unreachable", "timeout"})

VIEW = ToolSpec(
    "view",
    "View numbered lines of a Source file.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "start_line", "end_line"],
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": ["integer", "null"]},
            "end_line": {"type": ["integer", "null"]},
        },
    },
    read_only=True,
)


class ModelPortContract:
    """The shared suite; see the module docstring for the hooks a subclass provides."""

    max_output_path: ClassVar[tuple[str, ...]]
    forbidden_fields: ClassVar[frozenset[str]] = frozenset({"temperature", "top_p", "top_k"})
    ok_raw_args: ClassVar[str] = '{"path": "main.tex"}'
    expected_usage: ClassVar[StepUsage] = StepUsage(10, 5, cache_read_tokens=3)
    secret: ClassVar[str] = "sk-contract-secret-0000"

    # -- hooks -------------------------------------------------------------------

    def make_port(self, handler: Handler) -> ModelPort:
        raise NotImplementedError

    def ok_response(self) -> httpx.Response:
        raise NotImplementedError

    def text_prefix(self) -> bytes:
        raise NotImplementedError

    def error_response(self, kind: str) -> httpx.Response | Exception:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------------

    def call(self, *, max_output_tokens: int = 256) -> ModelCall:
        return ModelCall(
            system="sys",
            messages=(Message("user", (TextBlock("hi"),)),),
            tools=(VIEW,),
            max_output_tokens=max_output_tokens,
        )

    def answer_ok(self, bodies: list[Any]) -> Handler:
        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return self.ok_response()

        return handler

    def stream_headers(self) -> dict[str, str]:
        content_type = self.ok_response().headers.get("content-type")
        return {"content-type": content_type} if content_type else {}

    async def collect(self, port: ModelPort, call: ModelCall | None = None) -> list[ModelEvent]:
        with anyio.fail_after(30):
            return [event async for event in port.stream(call or self.call())]

    # -- the contract --------------------------------------------------------------

    @pytest.mark.anyio
    async def test_events_end_with_exactly_one_stopped(self) -> None:
        events = await self.collect(self.make_port(self.answer_ok([])))
        stops = [e for e in events if isinstance(e, Stopped)]
        assert len(stops) == 1 and events[-1] is stops[0]
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hi"
        [call] = [e for e in events if isinstance(e, ToolCallCompleted)]
        assert (call.name, call.parse_error, call.recovered) == ("view", None, False)
        assert stops[0].reason == "tool_use" and stops[0].message.role == "assistant"

    @pytest.mark.anyio
    async def test_raw_args_are_the_exact_received_string(self) -> None:
        events = await self.collect(self.make_port(self.answer_ok([])))
        [call] = [e for e in events if isinstance(e, ToolCallCompleted)]
        assert call.raw_args == self.ok_raw_args
        assert json.loads(call.raw_args) == {"path": "main.tex"}

    @pytest.mark.anyio
    async def test_max_output_tokens_is_always_sent(self) -> None:
        bodies: list[Any] = []
        port = self.make_port(self.answer_ok(bodies))
        for limit in (1, 777):
            await self.collect(port, self.call(max_output_tokens=limit))
        assert [_dig(body, self.max_output_path) for body in bodies] == [1, 777]

    @pytest.mark.anyio
    async def test_forbidden_sampling_is_never_sent(self) -> None:
        bodies: list[Any] = []
        await self.collect(self.make_port(self.answer_ok(bodies)))
        [body] = bodies
        assert not self.forbidden_fields & set(_keys(body))

    @pytest.mark.anyio
    async def test_usage_parses_cache_fields(self) -> None:
        events = await self.collect(self.make_port(self.answer_ok([])))
        [usage] = [e.usage for e in events if isinstance(e, UsageReport)]
        expected = self.expected_usage
        assert (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        ) == (
            expected.input_tokens,
            expected.output_tokens,
            expected.cache_read_tokens,
            expected.cache_write_tokens,
        )

    @pytest.mark.anyio
    async def test_closing_the_iterator_closes_the_stream_within_one_second(self) -> None:
        body = _OpenStream(self.text_prefix())
        port = self.make_port(
            lambda request: httpx.Response(200, headers=self.stream_headers(), stream=body)
        )
        events = port.stream(self.call())
        with anyio.fail_after(5):
            async for event in events:
                if isinstance(event, TextDelta):
                    break
        with anyio.fail_after(1):
            await _aclose(events)
        assert body.closed

    @pytest.mark.anyio
    @pytest.mark.parametrize("kind", ERROR_KINDS)
    async def test_errors_map_to_provider_error_codes(self, kind: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            reply = self.error_response(kind)  # fresh for every attempt
            if isinstance(reply, Exception):
                if isinstance(reply, httpx.RequestError):
                    reply.request = request
                raise reply
            return reply

        with pytest.raises(ProviderError) as caught:
            await self.collect(self.make_port(handler))
        error = caught.value
        assert error.code == kind and error.retryable is (kind in RETRYABLE)
        assert error.message and self.secret not in error.message
        assert self.secret not in str(error) and self.secret not in repr(error)

    @pytest.mark.anyio
    async def test_no_retry_after_the_first_streamed_byte(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            dropped = httpx.ReadError("connection reset", request=request)
            body = _OpenStream(self.text_prefix(), then=dropped)
            return httpx.Response(200, headers=self.stream_headers(), stream=body)

        received: list[ModelEvent] = []
        port = self.make_port(handler)
        with pytest.raises(ProviderError):
            with anyio.fail_after(30):
                async for event in port.stream(self.call()):
                    received.append(event)
        assert len(seen) == 1
        assert "".join(e.text for e in received if isinstance(e, TextDelta)) == "Hi"


class _OpenStream(httpx.AsyncByteStream):
    """Sends `first`, then raises `then` or stays open until closed."""

    def __init__(self, first: bytes, *, then: Exception | None = None) -> None:
        self._first = first
        self._then = then
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        if self._then is not None:
            raise self._then
        await anyio.sleep_forever()

    async def aclose(self) -> None:
        self.closed = True


def _dig(body: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        body = body.get(key) if isinstance(body, dict) else None
    return body


def _keys(value: Any) -> Iterator[str]:
    """Every object key anywhere in a JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


async def _aclose(stream: AsyncIterator[ModelEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()
