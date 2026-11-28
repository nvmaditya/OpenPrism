"""The Anthropic adapter (agent design §11.2), offline through httpx.MockTransport.

Stream fixtures under `tests/fixtures/providers/anthropic/` are written by hand from the
documented stream event shapes; no live call was made to build them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import anyio
import httpx
import pytest
from tests.agent.providers.contract import Handler, ModelPortContract

from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ProviderError,
    ProviderNotice,
    Stopped,
    TextBlock,
    ThinkingStarted,
    ToolCallCompleted,
    ToolResultBlock,
    ToolUseBlock,
    UsageReport,
    catalog,
)
from prismtex.agent.providers.anthropic import AnthropicModel
from prismtex.agent.tools import ToolSpec

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "anthropic"
VIEW = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "start_line", "end_line"],
    "properties": {
        "path": {"type": "string"},
        "start_line": {"type": ["integer", "null"]},
        "end_line": {"type": ["integer", "null"]},
    },
}
NO_ARGS = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
TOOLS = (
    ToolSpec("view", "View numbered lines.", VIEW, read_only=True),
    ToolSpec("check", "Compile the staged state.", NO_ARGS, read_only=True),
)
SONNET = catalog.spec("anthropic:claude-sonnet-5-5")
OPUS = catalog.spec("anthropic:claude-opus-5-5")
HAIKU = catalog.spec("anthropic:claude-haiku-5-5")
KEY = "sk-ant-test-0000"
BOUND = (
    "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a "
    "different conversation. Remove the block, or set `thinking.block_binding."
    'prefix_mismatch_behavior` to "drop_block".'
)

Requests = list[httpx.Request]


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK still retries (max_retries=2); tests skip its backoff sleep."""

    async def no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(anthropic.AsyncAnthropic, "_sleep_for_retry", no_sleep)


# -- helpers -------------------------------------------------------------------


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def sse(events: list[dict[str, Any]]) -> bytes:
    """Server-sent events in the wire format, one `event:` and `data:` pair per event."""
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def streamed(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def api_error(status: int, kind: str, message: str, **headers: str) -> httpx.Response:
    body = {"type": "error", "error": {"type": kind, "message": message}}
    return httpx.Response(status, headers=headers, json=body)


def serve(*replies: bytes | httpx.Response, seen: Requests) -> Handler:
    """Answer the n-th request with the n-th reply (the last one repeats)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies[min(len(seen), len(replies)) - 1]
        return streamed(reply) if isinstance(reply, bytes) else reply

    return handler


def model(
    handler: Handler,
    *,
    spec: Any = SONNET,
    ledger: Path | None = Path("ledger.jsonl"),
    **options: Any,
) -> AnthropicModel:
    return AnthropicModel(
        spec,
        api_key=KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ledger_path=ledger,
        **options,
    )


def user(text: str) -> Message:
    return Message("user", (TextBlock(text),))


def call(*messages: Message, tools: tuple[ToolSpec, ...] = TOOLS) -> ModelCall:
    return ModelCall(
        system="sys", messages=messages or (user("hi"),), tools=tools, max_output_tokens=16_000
    )


async def collect(port: ModelPort, request: ModelCall) -> list[ModelEvent]:
    with anyio.fail_after(30):
        return [event async for event in port.stream(request)]


def body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def stopped(events: list[ModelEvent]) -> Stopped:
    last = events[-1]
    assert isinstance(last, Stopped)
    return last


def tool_calls(events: list[ModelEvent]) -> list[ToolCallCompleted]:
    return [event for event in events if isinstance(event, ToolCallCompleted)]


THINKING_CONTENT = [
    {
        "type": "thinking",
        "thinking": "",
        "signature": "EqQBCkYIBhgCKkBfixturesignatureFQ3sY2xhdWRlLXNvbm5ldC01LTUiLCJmaXh0dXJlIjp0"
        "cnVlfQ7dGVzdC1vbmx5LW5vdC1hLXJlYWwtc2lnbmF0dXJl",
    },
    {"type": "text", "text": "Let me look at the file."},
    {
        "type": "tool_use",
        "id": "toolu_01ThinkingViewFixture0",
        "name": "view",
        "input": {"path": "main.tex", "start_line": 1, "end_line": None},
    },
]


# -- request shape -------------------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_payload_has_no_sampling_params_explicit_max_tokens_and_cache_breakpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://elsewhere.invalid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-environment")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-the-environment")
    seen: Requests = []
    port = model(serve(fixture("text_and_tool.sse"), seen=seen))
    events = await collect(port, call())
    [request] = seen
    sent = body(request)
    assert request.url.host == "api.anthropic.com" and request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == KEY and "authorization" not in request.headers
    assert "anthropic-beta" not in request.headers
    assert sent["model"] == "claude-sonnet-5-5" and sent["max_tokens"] == 16_000
    assert sent["stream"] is True
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & sent.keys()
    assert sent["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["thinking"] == {"type": "adaptive"} and sent["output_config"] == {
        "effort": "medium"
    }
    assert [t["name"] for t in sent["tools"]] == ["check", "view"]
    assert all(t["strict"] is True and t["eager_input_streaming"] is True for t in sent["tools"])
    assert sent["tools"][1] == {
        "name": "view",
        "description": "View numbered lines.",
        "input_schema": VIEW,
        "strict": True,
        "eager_input_streaming": True,
    }
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert isinstance(events[-1], Stopped)


@pytest.mark.anyio
async def test_anthropic_effort_is_sent_as_configured_and_an_unknown_effort_is_refused() -> None:
    seen: Requests = []
    port = model(serve(fixture("text_and_tool.sse"), seen=seen), effort="high")
    await collect(port, call())
    assert body(seen[0])["output_config"] == {"effort": "high"}
    with pytest.raises(ValueError, match="effort"):
        model(serve(seen=seen), effort="extreme")


@pytest.mark.anyio
async def test_anthropic_response_schema_becomes_the_output_format() -> None:
    seen: Requests = []
    schema = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
    port = model(serve(fixture("text_and_tool.sse"), seen=seen))
    request = ModelCall(system="sys", messages=(user("hi"),), response_schema=schema)
    await collect(port, request)
    sent = body(seen[0])
    assert sent["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": schema},
    }
    assert "tools" not in sent


# -- history -------------------------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_replays_native_content_verbatim_including_thinking_signatures() -> None:
    seen: Requests = []
    port = model(serve(fixture("thinking_text_tool.sse"), fixture("text_and_tool.sse"), seen=seen))
    first = await collect(port, call())
    assert any(isinstance(event, ThinkingStarted) for event in first)
    [view] = tool_calls(first)
    assert view.raw_args == '{"path": "main.tex", "start_line": 1, "end_line": null}'
    reply = stopped(first).message
    assert reply.native == {"role": "assistant", "content": THINKING_CONTENT}
    assert reply.blocks == (
        TextBlock("Let me look at the file."),
        ToolUseBlock(
            "toolu_01ThinkingViewFixture0",
            "view",
            {"path": "main.tex", "start_line": 1, "end_line": None},
        ),
    )
    results = Message(
        "user",
        (TextBlock("STOP: answer now."), ToolResultBlock(view.id, "1\t\\documentclass{article}")),
    )
    await collect(port, call(user("hi"), reply, results))
    first_sent, second_sent = (body(request)["messages"] for request in seen)
    assert second_sent[0] == first_sent[0]  # append-only
    assert second_sent[1] == {"role": "assistant", "content": THINKING_CONTENT}
    assert second_sent[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01ThinkingViewFixture0",
                "content": "1\t\\documentclass{article}",
            },
            {"type": "text", "text": "STOP: answer now."},
        ],
    }


@pytest.mark.anyio
async def test_anthropic_host_built_messages_render_from_their_blocks() -> None:
    seen: Requests = []
    port = model(serve(fixture("text_and_tool.sse"), seen=seen))
    assistant = Message(
        "assistant",
        (TextBlock("Checking."), ToolUseBlock("toolu_x", "check", {})),
    )
    failed = Message("user", (ToolResultBlock("toolu_x", "compile failed", is_error=True),))
    await collect(port, call(user("hi"), assistant, failed))
    assert body(seen[0])["messages"][1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "toolu_x", "name": "check", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_x",
                    "content": "compile failed",
                    "is_error": True,
                }
            ],
        },
    ]


def _history_with_thinking() -> ModelCall:
    reply = Message(
        "assistant",
        (TextBlock("Let me look at the file."),),
        native={"role": "assistant", "content": THINKING_CONTENT},
    )
    result = Message("user", (ToolResultBlock("toolu_01ThinkingViewFixture0", "1\tx"),))
    return call(user("hi"), reply, result)


@pytest.mark.anyio
async def test_anthropic_bound_history_error_strips_thinking_retries_once_then_history_rejected() -> (  # noqa: E501
    None
):
    seen: Requests = []
    bound = api_error(400, "invalid_request_error", BOUND)
    port = model(serve(bound, fixture("text_and_tool.sse"), seen=seen))
    events = await collect(port, _history_with_thinking())
    assert len(seen) == 2
    assert isinstance(events[0], ProviderNotice) and events[0].code == "history_reset"
    assert KEY not in events[0].message
    assert isinstance(events[-1], Stopped)
    first, retry = (body(request)["messages"] for request in seen)
    assert first[1]["content"][0]["type"] == "thinking"
    assert retry[1] == {"role": "assistant", "content": THINKING_CONTENT[1:]}
    assert retry[0] == first[0] and retry[2] == first[2]

    seen.clear()
    port = model(serve(bound, seen=seen))
    with pytest.raises(ProviderError) as caught:
        await collect(port, _history_with_thinking())
    assert caught.value.code == "history_rejected" and caught.value.retryable is True
    assert len(seen) == 2


class _HeldOpen(httpx.AsyncByteStream):
    """A response body that sends nothing and stays open until it is closed."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await anyio.sleep_forever()
        yield b""

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_anthropic_closing_after_the_history_notice_closes_the_retried_stream() -> None:
    held = _HeldOpen()
    seen: Requests = []
    bound = api_error(400, "invalid_request_error", BOUND)
    retried = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=held)
    events = model(serve(bound, retried, seen=seen)).stream(_history_with_thinking())
    with anyio.fail_after(5):
        first = await anext(events)
    assert isinstance(first, ProviderNotice) and len(seen) == 2
    with anyio.fail_after(1):
        await events.aclose()  # pyright: ignore[reportAttributeAccessIssue]
    assert held.closed


@pytest.mark.anyio
async def test_anthropic_strict_history_sends_the_binding_beta() -> None:
    seen: Requests = []
    port = model(serve(fixture("text_and_tool.sse"), seen=seen), strict_history=True)
    await collect(port, call())
    [request] = seen
    sent = body(request)
    assert request.url.path == "/v1/messages" and request.url.params["beta"] == "true"
    assert "thinking-binding-controls-2026-08-01" in request.headers["anthropic-beta"]
    assert sent["thinking"] == {
        "type": "adaptive",
        "block_binding": {"prefix_mismatch_behavior": "error"},
    }
    assert not {"temperature", "top_p", "top_k", "tool_choice", "fallbacks"} & sent.keys()

    seen.clear()
    port = model(
        serve(api_error(400, "invalid_request_error", BOUND), seen=seen), strict_history=True
    )
    with pytest.raises(ProviderError) as caught:
        await collect(port, _history_with_thinking())
    assert caught.value.code == "history_rejected"
    assert len(seen) == 1  # strict: never stripped and retried


# -- responses -----------------------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_refusal_maps_to_stopped_refusal_with_category() -> None:
    port = model(serve(fixture("refusal.sse"), seen=[]), spec=OPUS)
    events = await collect(port, call())
    stop = stopped(events)
    assert (stop.reason, stop.refusal_category) == ("refusal", "cyber")
    assert stop.message.blocks == ()
    assert [block["type"] for block in (stop.message.native or {})["content"]] == ["thinking"]


@pytest.mark.anyio
async def test_anthropic_usage_counts_cache_writes_and_reads_and_iterations_by_served_model() -> (
    None
):
    port = model(serve(fixture("usage_iterations.sse"), seen=[]), spec=OPUS)
    events = await collect(port, call())
    [usage] = [event.usage for event in events if isinstance(event, UsageReport)]
    assert (usage.input_tokens, usage.output_tokens) == (100, 300)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (5000, 2000)
    assert usage.served_model == "claude-opus-5-5"
    assert [
        (u.served_model, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens)
        for u in usage.iterations
    ] == [("claude-opus-5-5", 40, 20, 5000, 2000), ("claude-opus-5", 60, 280, 0, 0)]
    stop = stopped(events)
    assert stop.reason == "end" and stop.message.blocks == (TextBlock("Done."),)
    native_content = (stop.message.native or {})["content"]
    assert native_content[0] == {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-5"},
        "trigger": {"type": "refusal", "category": "cyber"},
    }
    assert isinstance(events[-2], UsageReport)


@pytest.mark.anyio
async def test_anthropic_max_tokens_stop_marks_the_cut_off_tool_call_truncated() -> None:
    port = model(serve(fixture("max_tokens_tool.sse"), seen=[]), spec=HAIKU)
    events = await collect(port, call())
    [cut] = tool_calls(events)
    assert (cut.name, cut.parse_error, cut.args) == ("write", "truncated", None)
    assert cut.raw_args == '{"path": "intro.tex", "text": "\\\\section{Introduction}\\nWe study'
    stop = stopped(events)
    assert stop.reason == "max_tokens"
    assert stop.message.blocks[0] == TextBlock("Writing the section.")
    assert stop.message.blocks[1] == ToolUseBlock("toolu_01TruncatedFixture00000", "write", {})

    unclosed = fixture("max_tokens_tool.sse").replace(
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n', b""
    )
    events = await collect(model(serve(unclosed, seen=[]), spec=HAIKU), call())
    [cut] = tool_calls(events)
    assert cut.parse_error == "truncated" and stopped(events).reason == "max_tokens"


def _tool_stream(partial_json: list[str], stop_reason: str = "tool_use") -> bytes:
    start = {"type": "tool_use", "id": "toolu_latex", "name": "write", "input": {}}
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_latex",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": start},
    ]
    for part in partial_json:
        delta = {"type": "input_json_delta", "partial_json": part}
        events.append({"type": "content_block_delta", "index": 0, "delta": delta})
    events += [
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]
    return sse(events)


@pytest.mark.anyio
async def test_anthropic_single_escaped_latex_in_tool_input_streams_intact() -> None:
    raw = '{"path": "a.tex", "text": "\\section{A} \\label{s}"}'
    port = model(serve(_tool_stream([raw[:20], raw[20:31], raw[31:]]), seen=[]))
    events = await collect(port, call())
    [write] = tool_calls(events)
    assert write.raw_args == raw and write.parse_error is None
    stop = stopped(events)
    assert stop.reason == "tool_use"
    # The replayable input is the leniently parsed object: the backslashes survive.
    native_input = (stop.message.native or {})["content"][0]["input"]
    assert native_input == {"path": "a.tex", "text": "\\section{A} \\label{s}"}


@pytest.mark.anyio
async def test_anthropic_tool_without_arguments_has_empty_object_args() -> None:
    events = await collect(model(serve(_tool_stream([]), seen=[])), call())
    [write] = tool_calls(events)
    assert (write.raw_args, write.args) == ("{}", {})


@pytest.mark.anyio
async def test_anthropic_stream_cut_before_message_stop_is_an_error() -> None:
    whole = fixture("text_and_tool.sse")
    cut = whole[: whole.index(b"event: message_delta")]
    with pytest.raises(ProviderError) as caught:
        await collect(model(serve(cut, seen=[])), call())
    assert caught.value.code == "unreachable"


# -- errors --------------------------------------------------------------------


def _mid_stream_error(kind: str) -> bytes:
    whole = fixture("text_and_tool.sse")
    head = whole[: whole.index(b"event: content_block_stop")]
    error = {"type": "error", "error": {"type": kind, "message": "Overloaded"}}
    return head + f"event: error\ndata: {json.dumps(error)}\n\n".encode()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "code", "attempts"),
    [
        (
            api_error(400, "invalid_request_error", "prompt is too long: 1000001 tokens"),
            "context_overflow",
            1,
        ),
        (
            api_error(413, "request_too_large", "Request exceeds the maximum size"),
            "context_overflow",
            1,
        ),
        (api_error(400, "invalid_request_error", "messages.0: bad block"), "bad_request", 1),
        (api_error(403, "permission_error", "no access"), "auth", 1),
        (api_error(404, "not_found_error", "model: claude-x"), "not_found", 1),
        (api_error(529, "overloaded_error", "Overloaded"), "overloaded", 3),
        (api_error(500, "api_error", "Internal server error"), "overloaded", 3),
        (api_error(504, "timeout_error", "Request timed out"), "timeout", 3),
        (_mid_stream_error("overloaded_error"), "overloaded", 1),
    ],
)
async def test_anthropic_errors_map_to_provider_error_codes(
    reply: httpx.Response | bytes, code: str, attempts: int
) -> None:
    seen: Requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(reply, bytes):
            return streamed(reply)
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)

    with pytest.raises(ProviderError) as caught:
        await collect(model(handler), call())
    assert caught.value.code == code and len(seen) == attempts
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)


@pytest.mark.anyio
async def test_anthropic_bad_request_message_carries_the_provider_reason() -> None:
    reply = api_error(400, "invalid_request_error", "messages.3: tool_use ids without tool_result")
    with pytest.raises(ProviderError) as caught:
        await collect(model(serve(reply, seen=[])), call())
    assert "tool_use ids without tool_result" in caught.value.message


@pytest.mark.anyio
async def test_anthropic_rate_limit_carries_retry_after_seconds() -> None:
    seen: Requests = []
    reply = api_error(429, "rate_limit_error", "slow down", **{"retry-after": "7"})
    with pytest.raises(ProviderError) as caught:
        await collect(model(serve(reply, seen=seen)), call())
    assert caught.value.code == "rate_limited" and caught.value.retry_after_s == 7.0
    assert len(seen) == 3  # the SDK's max_retries=2, all before the first streamed byte


# -- counting and construction ---------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_count_tokens_uses_the_free_endpoint() -> None:
    seen: Requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"input_tokens": 1234})

    port = model(handler)
    assert await port.count_tokens(call()) == 1234
    [request] = seen
    sent = body(request)
    assert request.url.host == "api.anthropic.com"
    assert request.url.path == "/v1/messages/count_tokens"
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert [t["name"] for t in sent["tools"]] == ["check", "view"]
    assert sent["system"][0]["text"] == "sys"
    assert not {"max_tokens", "stream", "temperature", "top_p", "top_k"} & sent.keys()


@pytest.mark.anyio
async def test_anthropic_count_tokens_is_none_for_a_bound_history() -> None:
    port = model(serve(api_error(400, "invalid_request_error", BOUND), seen=[]))
    assert await port.count_tokens(_history_with_thinking()) is None


@pytest.mark.anyio
async def test_anthropic_fallbacks_beta_only_when_enabled_and_never_for_haiku() -> None:
    async def sent_with(spec: Any, fallbacks: bool) -> httpx.Request:
        seen: Requests = []
        handler = serve(fixture("text_and_tool.sse"), seen=seen)
        await collect(model(handler, spec=spec, fallbacks=fallbacks), call())
        [request] = seen
        return request

    for spec in (OPUS, SONNET):
        request = await sent_with(spec, True)
        assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
        assert request.url.params["beta"] == "true" and body(request)["fallbacks"] == "default"
    for spec, fallbacks in ((OPUS, False), (HAIKU, True)):
        request = await sent_with(spec, fallbacks)
        assert "anthropic-beta" not in request.headers and "fallbacks" not in body(request)


@pytest.mark.anyio
async def test_anthropic_fallbacks_and_strict_history_share_one_beta_request() -> None:
    seen: Requests = []
    port = model(
        serve(fixture("text_and_tool.sse"), seen=seen),
        spec=OPUS,
        fallbacks=True,
        strict_history=True,
    )
    await collect(port, call())
    [request] = seen
    betas = request.headers["anthropic-beta"].split(",")
    assert {"thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"} <= set(betas)


def test_anthropic_model_without_a_ledger_cannot_be_constructed() -> None:
    seen: Requests = []
    with pytest.raises(ValueError) as caught:
        model(serve(fixture("text_and_tool.sse"), seen=seen), ledger=None)
    assert str(caught.value) == (
        "a cost ledger is required for paid calls; set PRISMTEX_LEDGER_PATH"
    )
    assert seen == []


def test_anthropic_model_refuses_other_providers_and_an_empty_key() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(serve(seen=[])))
    with pytest.raises(ValueError, match="anthropic"):
        AnthropicModel(catalog.FAKE_SPEC, api_key=KEY, http_client=client, ledger_path=Path("l"))
    with pytest.raises(ValueError, match="API key"):
        AnthropicModel(SONNET, api_key="", http_client=client, ledger_path=Path("l"))


def test_anthropic_model_repr_never_shows_the_key() -> None:
    port = model(serve(seen=[]))
    assert KEY not in repr(port) and "claude-sonnet-5-5" in repr(port)
    assert port.ledger_path == Path("ledger.jsonl")


# -- the shared adapter contract -------------------------------------------------


class TestAnthropicContract(ModelPortContract):
    max_output_path = ("max_tokens",)
    forbidden_fields = frozenset({"temperature", "top_p", "top_k", "tool_choice"})

    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path: Path) -> Iterator[None]:
        self.ledger = tmp_path / "ledger.jsonl"
        yield
        assert not self.ledger.exists()  # the adapter never writes the ledger itself

    def make_port(self, handler: Callable[[httpx.Request], httpx.Response]) -> ModelPort:
        return AnthropicModel(
            SONNET,
            api_key=self.secret,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ledger_path=self.ledger,
        )

    def ok_response(self) -> httpx.Response:
        return streamed(fixture("text_and_tool.sse"))

    def text_prefix(self) -> bytes:
        whole = fixture("text_and_tool.sse")
        marker = b'"text_delta","text":"Hi"}}\n\n'
        return whole[: whole.index(marker) + len(marker)]

    def error_response(self, kind: str) -> httpx.Response | Exception:
        if kind == "unreachable":
            return httpx.ConnectError("connection refused")
        if kind == "timeout":
            return httpx.ReadTimeout("timed out")
        status, error_type = {
            "auth": (401, "authentication_error"),
            "not_found": (404, "not_found_error"),
            "rate_limited": (429, "rate_limit_error"),
            "overloaded": (529, "overloaded_error"),
        }[kind]
        return api_error(status, error_type, f"{error_type} for test", **{"retry-after": "0"})
