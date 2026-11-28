"""The native Ollama adapter (agent design §11.4).

Every request goes through `httpx.MockTransport`; the streams in
`fixtures/providers/ollama/` are written by hand from Ollama's documented `/api/chat`
response shapes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from tests.agent.providers.contract import VIEW, Handler, ModelPortContract

from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ProviderError,
    StepUsage,
    Stopped,
    TextBlock,
    TextDelta,
    ToolCallCompleted,
    ToolResultBlock,
    ToolUseBlock,
    UsageReport,
    catalog,
)
from prismtex.agent.providers.ollama import OllamaModel, list_models, show_capabilities
from prismtex.agent.tools import ToolSpec

BASE_URL = "http://127.0.0.1:11434"
FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "ollama"

OK_NDJSON = (
    b'{"message":{"role":"assistant","content":"Hi"},"done":false}\n'
    b'{"message":{"role":"assistant","content":""},"done":true,"done_reason":"stop",'
    b'"prompt_eval_count":12,"eval_count":2}\n'
)

EDIT = ToolSpec(
    "edit",
    "Replace text in a Source file.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "old_str", "new_str"],
        "properties": {
            "path": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
        },
    },
    read_only=False,
    latex_fields=frozenset({"old_str", "new_str"}),
)

CALL = ModelCall(system="sys", messages=(Message("user", (TextBlock("hi"),)),))


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def mock_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def port_for(handler: Handler, ref: str = catalog.OLLAMA_DEFAULT) -> OllamaModel:
    spec = catalog.spec(ref)
    assert spec.num_ctx is not None
    return OllamaModel(
        spec, base_url=BASE_URL, client=mock_client(handler), num_ctx=spec.num_ctx, retry_delay_s=0
    )


def ndjson(*lines: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def text(content: str) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": content}, "done": False}


def done(prompt: int, output: int, reason: str = "stop") -> dict[str, Any]:
    return {
        "model": "qwen3:4b-instruct-2507-q4_K_M",
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": reason,
        "prompt_eval_count": prompt,
        "eval_count": output,
    }


def compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


async def collect(port: OllamaModel, call: ModelCall = CALL) -> list[ModelEvent]:
    return [event async for event in port.stream(call)]


@pytest.mark.anyio
async def test_ollama_payload_pins_num_ctx_and_maps_context_errors() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(200, content=OK_NDJSON)
        return httpx.Response(400, json={"error": "the input length exceeds the context length"})

    port = OllamaModel(
        catalog.spec(catalog.OLLAMA_DEFAULT),
        base_url=BASE_URL,
        client=mock_client(handler),
        num_ctx=16384,
    )
    call = ModelCall(
        system="sys",
        messages=(Message("user", (TextBlock("hi"),)),),
        max_output_tokens=1756,
        seed=7,
    )
    [e async for e in port.stream(call)]
    body = seen[0]
    assert body["model"] == "qwen3:4b-instruct-2507-q4_K_M"
    assert body["stream"] is True and body["keep_alive"] == "30m"
    assert body["options"] == {
        "num_ctx": 16384,
        "num_predict": 1756,
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "seed": 7,
    }
    assert "tools" not in body and "format" not in body
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    with pytest.raises(ProviderError) as exc:
        [e async for e in port.stream(call)]
    assert exc.value.code == "context_overflow" and seen[1]["options"]["num_ctx"] == 16384
    assert len(seen) == 2  # a context error is not retried


@pytest.mark.anyio
async def test_ollama_flags_a_response_that_filled_the_window() -> None:
    replies = [ndjson(text("Hi"), done(16_000, 352)), ndjson(text("Hi"), done(16_000, 351))]
    port = port_for(lambda request: httpx.Response(200, content=replies.pop(0)))

    events: list[ModelEvent] = []
    with pytest.raises(ProviderError) as caught:
        async for event in port.stream(CALL):
            events.append(event)
    assert caught.value.code == "context_overflow"
    assert "16352 of 16384" in caught.value.message
    # The usage is still reported, so the Turn counts the tokens it spent.
    assert [type(event) for event in events] == [TextDelta, UsageReport]
    usage = events[-1]
    assert isinstance(usage, UsageReport)
    assert (usage.usage.input_tokens, usage.usage.output_tokens) == (16_000, 352)

    below = await collect(port)
    assert isinstance(below[-1], Stopped) and below[-1].reason == "end"


@pytest.mark.anyio
async def test_ollama_tool_calls_are_reserialized_canonically_and_stop_is_tool_use() -> None:
    stream = fixture("two_tool_calls.ndjson")
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, content=stream if len(bodies) == 1 else OK_NDJSON)

    port = port_for(handler)
    call = ModelCall(
        system="sys",
        messages=(Message("user", (TextBlock("Fix the heading."),)),),
        tools=(EDIT, VIEW),
    )
    events = await collect(port, call)

    edit, view = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert edit.raw_args == (
        '{"new_str":"\\\\section{Introducción}","old_str":"\\\\section{Intro}","path":"main.tex"}'
    )
    assert view.raw_args == '{"end_line":null,"path":"main.tex","start_line":1}'
    assert json.loads(edit.raw_args) == edit.args and json.loads(view.raw_args) == view.args
    assert edit.id == "call_7a1b9c" and view.id and view.id != edit.id
    assert [(c.parse_error, c.recovered) for c in (edit, view)] == [(None, False), (None, False)]
    stop = events[-1]
    assert isinstance(stop, Stopped) and stop.reason == "tool_use"
    assert stop.message.blocks[0] == TextBlock("Fixing the heading.")
    assert [
        (block.id, block.name, block.input)
        for block in stop.message.blocks
        if isinstance(block, ToolUseBlock)
    ] == [(edit.id, "edit", edit.args), (view.id, "view", view.args)]

    # The next Step replays the reply as received and sends each result as a tool message.
    results = Message(
        "user",
        (
            ToolResultBlock(edit.id, "Edited main.tex."),
            ToolResultBlock(view.id, "1 \\section{Introducción}", is_error=False),
            TextBlock("Answer briefly."),
        ),
    )
    await collect(port, replace(call, messages=(*call.messages, stop.message, results)))
    received = json.loads(stream.splitlines()[2])["message"]["tool_calls"]
    assert compact(received) in stream and compact(received) in bodies[1]  # byte for byte
    assert json.loads(bodies[1])["messages"][2:] == [
        {"role": "assistant", "content": "Fixing the heading.", "tool_calls": received},
        {"role": "tool", "content": "Edited main.tex.", "tool_name": "edit"},
        {"role": "tool", "content": "1 \\section{Introducción}", "tool_name": "view"},
        {"role": "user", "content": "Answer briefly."},
    ]


@pytest.mark.anyio
async def test_ollama_tools_only_for_agentic_steps_and_format_only_for_router_steps() -> None:
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=OK_NDJSON)

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent"],
        "properties": {"intent": {"enum": ["edit", "question"]}},
    }
    port = port_for(handler)
    for call in (
        replace(CALL, tools=(VIEW,)),
        replace(CALL, response_schema=MappingProxyType(schema)),
        CALL,
    ):
        await collect(port, call)

    agentic, router, plain = bodies
    assert agentic["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "view",
                "description": VIEW.description,
                "parameters": VIEW.input_schema,
            },
        }
    ]
    assert "format" not in agentic
    assert router["format"] == schema and "tools" not in router
    assert "tools" not in plain and "format" not in plain


@pytest.mark.anyio
async def test_ollama_unreachable_and_missing_model_messages_say_what_to_do() -> None:
    model = "qwen3:4b-instruct-2507-q4_K_M"
    attempts: list[str] = []

    def refused(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderError) as down:
        await collect(port_for(refused))
    assert down.value.code == "unreachable" and down.value.retryable
    assert "Start Ollama, or set PRISMTEX_OLLAMA_URL" in down.value.message
    assert attempts == ["/api/chat", "/api/chat"]  # two attempts, nothing was streamed
    with pytest.raises(ProviderError) as probe:
        await list_models(BASE_URL, mock_client(refused))
    assert probe.value.code == "unreachable"
    assert "Start Ollama, or set PRISMTEX_OLLAMA_URL" in probe.value.message

    attempts.clear()

    def missing(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        return httpx.Response(404, json={"error": f"model '{model}' not found"})

    with pytest.raises(ProviderError) as absent:
        await collect(port_for(missing))
    assert absent.value.code == "not_found" and not absent.value.retryable
    assert f"Run: ollama pull {model}" in absent.value.message
    with pytest.raises(ProviderError) as shown:
        await show_capabilities(BASE_URL, model, mock_client(missing))
    assert f"Run: ollama pull {model}" in shown.value.message
    assert attempts == ["/api/chat", "/api/show"]  # a missing model is not retried


@pytest.mark.anyio
async def test_ollama_unknown_model_reads_tools_from_api_show_and_defaults_to_8192() -> None:
    spec = catalog.spec("ollama:mistral-nemo:12b")
    assert (spec.context_window, spec.num_ctx, spec.native_tools) == (8192, 8192, False)
    assert dict(spec.sampling) == {}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "modelfile": "FROM mistral-nemo:12b",
                    "details": {"family": "llama", "parameter_size": "12.2B"},
                    "capabilities": ["completion", "tools"],
                },
            )
        return httpx.Response(200, content=OK_NDJSON)

    client = mock_client(handler)
    capabilities = await show_capabilities(BASE_URL, "mistral-nemo:12b", client)
    assert capabilities == frozenset({"completion", "tools"})
    assert requests[0].method == "POST" and requests[0].url.path == "/api/show"
    assert json.loads(requests[0].content) == {"model": "mistral-nemo:12b"}

    port = OllamaModel(
        replace(spec, native_tools="tools" in capabilities),
        base_url=BASE_URL,
        client=client,
        num_ctx=8192,
    )
    assert port.spec.native_tools and port.spec.context_window == 8192
    await collect(port)
    assert json.loads(requests[1].content)["options"] == {"num_ctx": 8192, "num_predict": 1024}
    assert await port.count_tokens(CALL) is None

    too_old = mock_client(lambda request: httpx.Response(200, json={"modelfile": ""}))
    assert await show_capabilities(BASE_URL, "mistral-nemo:12b", too_old) == frozenset()


@pytest.mark.anyio
async def test_ollama_list_models_reads_api_tags_with_and_without_latest() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3:4b-instruct-2507-q4_K_M", "digest": "sha256:1", "size": 2},
                    {"name": "llama3.1:latest", "digest": "sha256:2", "size": 4},
                ]
            },
        )

    names = await list_models(BASE_URL + "/", mock_client(handler))
    assert names == {"qwen3:4b-instruct-2507-q4_K_M", "llama3.1:latest", "llama3.1"}
    assert (seen[0].method, str(seen[0].url)) == ("GET", f"{BASE_URL}/api/tags")


@pytest.mark.anyio
async def test_ollama_length_stop_is_max_tokens_and_an_error_line_is_not_retried() -> None:
    port = port_for(lambda request: httpx.Response(200, content=ndjson(done(40, 9, "length"))))
    [usage, stop] = await collect(port)
    assert isinstance(usage, UsageReport) and usage.usage == StepUsage(
        40, 9, served_model="qwen3:4b-instruct-2507-q4_K_M"
    )
    assert isinstance(stop, Stopped) and stop.reason == "max_tokens"

    seen: list[httpx.Request] = []

    def failing(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=ndjson(text("Hi"), {"error": "runner terminated"}))

    received: list[ModelEvent] = []
    with pytest.raises(ProviderError) as caught:
        async for event in port_for(failing).stream(CALL):
            received.append(event)
    assert caught.value.code == "overloaded" and len(seen) == 1
    assert received == [TextDelta("Hi")]


def test_ollama_spec_window_follows_the_pinned_num_ctx() -> None:
    spec = catalog.spec(catalog.OLLAMA_DEFAULT)
    client = mock_client(lambda request: httpx.Response(200))
    port = OllamaModel(spec, base_url=BASE_URL, client=client, num_ctx=12_288)
    assert (port.spec.context_window, port.spec.num_ctx) == (12_288, 12_288)
    assert OllamaModel(spec, base_url=BASE_URL, client=client, num_ctx=16_384).spec is spec
    with pytest.raises(ValueError, match="PRISMTEX_OLLAMA_URL"):
        OllamaModel(spec, base_url="127.0.0.1:11434", client=client, num_ctx=16_384)


class TestOllamaContract(ModelPortContract):
    """The shared adapter contract, over the `ollama:*` default spec (no sampling)."""

    max_output_path = ("options", "num_predict")
    ok_raw_args = '{"path":"main.tex"}'  # re-serialized as canonical JSON
    expected_usage = StepUsage(10, 5)  # Ollama reports no cache reads or writes

    def make_port(self, handler: Handler) -> OllamaModel:
        return OllamaModel(
            catalog.spec("ollama:contract-model"),
            base_url=BASE_URL,
            client=mock_client(handler),
            num_ctx=8192,
            retry_delay_s=0,
        )

    def ok_response(self) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=fixture("text_and_tool_call.ndjson"),
        )

    def text_prefix(self) -> bytes:
        first, _, _ = fixture("text_and_tool_call.ndjson").partition(b"\n")
        return first + b"\n"

    def error_response(self, kind: str) -> httpx.Response | Exception:
        if kind == "auth":
            return httpx.Response(401, json={"error": "unauthorized"})
        if kind == "not_found":
            return httpx.Response(404, json={"error": "model 'contract-model' not found"})
        if kind == "rate_limited":
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        if kind == "overloaded":
            return httpx.Response(
                503, json={"error": "server busy, please try again. maximum pending requests"}
            )
        if kind == "unreachable":
            return httpx.ConnectError("connection refused")
        if kind == "timeout":
            return httpx.ReadTimeout("timed out")
        raise AssertionError(kind)
