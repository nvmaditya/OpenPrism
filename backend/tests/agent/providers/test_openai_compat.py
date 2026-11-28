"""The OpenAI-compatible adapter (agent design §11.3), offline through httpx.MockTransport.

Streams are written by hand from the documented `chat.completion.chunk` shape; no live
call was made to build them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import httpx
import openai
import pytest
from tests.agent.providers.contract import Handler, ModelPortContract

from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ModelUnavailable,
    ProviderError,
    Stopped,
    TextBlock,
    TextDelta,
    ThinkingStarted,
    ToolCallCompleted,
    ToolResultBlock,
    ToolUseBlock,
    UsageReport,
)
from prismtex.agent.providers.catalog import PRESETS, ProviderRegistry, spec
from prismtex.agent.providers.openai_compat import OpenAICompatModel
from prismtex.agent.tools import ToolSpec
from prismtex.settings import Settings


def chunk(
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    obj: dict[str, Any] = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m",
        "choices": []
        if delta is None and finish is None
        else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage:
        obj["usage"] = usage
    return f"data: {json.dumps(obj)}\n\n"


def fn(index: int, args: str, id: str | None = None, name: str | None = None) -> dict[str, Any]:
    call: dict[str, Any] = {"index": index, "function": {"arguments": args}}
    if id:
        call.update(id=id, type="function")
        call["function"]["name"] = name
    return {"tool_calls": [call]}


STREAM = (
    "".join(
        [
            chunk({"role": "assistant", **fn(0, '{"pa', id="call_a", name="view")}),
            chunk(fn(1, '{"query":', id="call_b", name="search")),
            chunk(fn(0, 'th": "main.tex"}')),
            chunk(fn(1, ' "x"}')),
            chunk(finish="tool_calls"),
            chunk(
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": 7,
                    "total_tokens": 27,
                    "prompt_tokens_details": {"cached_tokens": 4},
                }
            ),
        ]
    )
    + "data: [DONE]\n\n"
)


@pytest.mark.anyio
async def test_openai_compat_assembles_streamed_tool_arguments_by_index(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=STREAM.encode()
            )
        )
    )
    port = OpenAICompatModel(
        spec("openai:gpt-test"),
        preset=PRESETS["openai"],
        base_url="https://api.openai.com/v1",
        api_key="sk-o",
        http_client=client,
        ledger_path=tmp_path / "ledger.jsonl",  # openai is a paid preset
    )
    call = ModelCall(
        system="s", messages=(Message("user", (TextBlock("go"),)),), max_output_tokens=512
    )
    events = [e async for e in port.stream(call)]
    calls = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert [(c.id, c.name, c.raw_args) for c in calls] == [
        ("call_a", "view", '{"path": "main.tex"}'),
        ("call_b", "search", '{"query": "x"}'),
    ]
    [usage] = [e.usage for e in events if isinstance(e, UsageReport)]
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (16, 4, 7)
    assert isinstance(events[-1], Stopped) and events[-1].reason == "tool_use"


# -- helpers -------------------------------------------------------------------

BASE = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "vllm": "http://127.0.0.1:8001/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}
VIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {"path": {"type": "string"}},
}
VIEW = ToolSpec("view", "View numbered lines.", VIEW_SCHEMA, read_only=True)
USAGE = {"prompt_tokens": 13, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3}}
OK = (
    "".join(
        [
            chunk({"role": "assistant", "content": "Hi"}),
            chunk(fn(0, '{"path": ', id="call_v", name="view")),
            chunk(fn(0, '"main.tex"}')),
            chunk(finish="tool_calls"),
            chunk(usage=USAGE),
        ]
    )
    + "data: [DONE]\n\n"
)

Requests = list[httpx.Request]


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK still retries (max_retries=2); tests skip its backoff sleep."""

    async def no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(openai.AsyncOpenAI, "_sleep_for_retry", no_sleep)


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def sse(*chunks: str, done: bool = True) -> bytes:
    return ("".join(chunks) + ("data: [DONE]\n\n" if done else "")).encode()


def streamed(body: bytes | str) -> httpx.Response:
    content = body.encode() if isinstance(body, str) else body
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


def serve(*replies: bytes | str | httpx.Response, seen: Requests | None = None) -> Handler:
    """Answer the n-th request with the n-th reply (the last one repeats)."""
    log: Requests = seen if seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        reply = replies[min(len(log), len(replies)) - 1]
        return reply if isinstance(reply, httpx.Response) else streamed(reply)

    return handler


def model(
    handler: Handler,
    preset: str = "openai",
    *,
    ledger: Path | None = Path("ledger.jsonl"),
    key: str | None = "sk-test-0000",
    **changes: Any,
) -> OpenAICompatModel:
    row = spec(f"{preset}:m-1")
    return OpenAICompatModel(
        replace(row, **changes) if changes else row,
        preset=PRESETS[preset],
        base_url=BASE[preset],
        api_key=key,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ledger_path=ledger,
    )


def user(text: str) -> Message:
    return Message("user", (TextBlock(text),))


def make_call(
    *messages: Message, tools: tuple[ToolSpec, ...] = (VIEW,), **options: Any
) -> ModelCall:
    return ModelCall(system="sys", messages=messages or (user("hi"),), tools=tools, **options)


async def collect(port: ModelPort, request: ModelCall | None = None) -> list[ModelEvent]:
    with anyio.fail_after(30):
        return [event async for event in port.stream(request or make_call())]


def bodies_of(seen: Requests) -> list[dict[str, Any]]:
    return [json.loads(request.content) for request in seen]


def api_error(status: int, body: Any, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers, json=body)


# -- keys and requests -----------------------------------------------------------


@pytest.mark.anyio
async def test_openai_compat_never_sends_a_key_to_another_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other_tools = {
        "OPENAI_API_KEY": "sk-o",
        "OPENAI_BASE_URL": "https://elsewhere.example/v1",
        "OPENAI_ORG_ID": "org-o",
        "OPENAI_PROJECT_ID": "proj-o",
        "OPENAI_ADMIN_KEY": "sk-admin-o",
        "OPENAI_CUSTOM_HEADERS": "X-Other-Tool: sk-h",
    }
    for name, value in other_tools.items():
        monkeypatch.setenv(name, value)
    seen: Requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return streamed(OK)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    endpoint: dict[str, Any] = {
        "openai_preset": "vllm",
        "openai_model": "qwen",
        "openai_base_url": "http://127.0.0.1:8001/v1/",
    }

    def leaks(request: httpx.Request) -> list[str]:
        sent = [str(request.url), *request.headers.values()]
        return [value for value in other_tools.values() if any(value in part for part in sent)]

    # No key set: the vllm preset sends "unused", never a key meant for another tool.
    registry = ProviderRegistry(settings(**endpoint), ledger_path=None, http_client=client)
    await collect(registry.resolve("vllm:qwen", None))
    [request] = seen
    assert str(request.url) == "http://127.0.0.1:8001/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer unused"
    assert "openai-organization" not in request.headers
    assert "openai-project" not in request.headers and "x-other-tool" not in request.headers
    assert leaks(request) == []

    # The configured key goes to the configured base URL and nowhere else.
    seen.clear()
    keyed = settings(openai_api_key="sk-v", anthropic_api_key="sk-ant-a", **endpoint)
    registry = ProviderRegistry(keyed, ledger_path=tmp_path / "ledger.jsonl", http_client=client)
    await collect(registry.resolve(None, "vllm:qwen"))
    await registry.health(probe=True)
    chat = [r for r in seen if r.url.path.endswith("/chat/completions")]
    others = [r for r in seen if r not in chat]
    assert [str(r.url) for r in chat] == ["http://127.0.0.1:8001/v1/chat/completions"]
    assert chat[0].headers["authorization"] == "Bearer sk-v" and leaks(chat[0]) == []
    assert [r.url.path for r in others] == ["/api/tags"]
    assert all("sk-v" not in value for r in others for value in r.headers.values())
    with pytest.raises(ModelUnavailable) as caught:
        registry.resolve("openrouter:vendor/model", None)  # another endpoint: never this key
    assert (caught.value.code, caught.value.env_var) == (
        "model_unavailable",
        "PRISMTEX_OPENAI_MODEL",
    )
    assert "sk-v" not in caught.value.message

    # A preset with a fixed base URL refuses any other one.
    with pytest.raises(ValueError, match="sends its key only to https://api.openai.com/v1"):
        OpenAICompatModel(
            spec("openai:gpt-test"),
            preset=PRESETS["openai"],
            base_url="https://proxy.example/v1",
            api_key="sk-o",
            http_client=client,
            ledger_path=tmp_path / "ledger.jsonl",
        )
    assert set(PRESETS) == {"openai", "openrouter", "vllm", "lmstudio"}
    assert "groq" not in PRESETS


@pytest.mark.anyio
async def test_openai_compat_finish_reasons_and_max_tokens_param_per_preset() -> None:
    for finish, reason in [
        ("stop", "end"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
    ]:
        body = sse(chunk({"role": "assistant", "content": "ok"}), chunk(finish=finish))
        events = await collect(model(serve(body)))
        assert isinstance(events[-1], Stopped) and events[-1].reason == reason, finish

    expected = {
        "openai": "max_completion_tokens",
        "openrouter": "max_tokens",
        "vllm": "max_tokens",
        "lmstudio": "max_tokens",
    }
    for name, param in expected.items():
        seen: Requests = []
        port = model(serve(OK, seen=seen), name)
        await collect(port, make_call(max_output_tokens=321))
        await collect(port, make_call(tools=(), max_output_tokens=7))
        with_tools, without = bodies_of(seen)
        other = ({"max_tokens", "max_completion_tokens"} - {param}).pop()
        assert (with_tools[param], without[param]) == (321, 7) and other not in with_tools
        assert with_tools["stream"] is True
        assert with_tools["stream_options"] == {"include_usage": True}
        assert with_tools["model"] == "m-1"
        if PRESETS[name].parallel_tool_calls:
            assert with_tools["parallel_tool_calls"] is False
        else:
            assert "parallel_tool_calls" not in with_tools
        assert "parallel_tool_calls" not in without and "tools" not in without
        assert not {"temperature", "top_p", "top_k", "tool_choice", "seed"} & set(with_tools)
    assert PRESETS["openai"].parallel_tool_calls and PRESETS["openrouter"].parallel_tool_calls


@pytest.mark.anyio
async def test_openai_compat_response_schema_seed_and_catalog_sampling() -> None:
    seen: Requests = []
    schema = {"type": "object", "properties": {"intent": {"type": "string"}}}
    port = model(serve(OK, seen=seen), "vllm", sampling={"temperature": 0.2, "top_k": 20})
    await collect(port, make_call(tools=(), response_schema=schema, seed=7))
    [body] = bodies_of(seen)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": schema},
    }
    assert (body["seed"], body["temperature"], body["top_k"]) == (7, 0.2, 20)


@pytest.mark.anyio
async def test_openai_compat_replays_native_messages_and_sends_tool_results_first() -> None:
    seen: Requests = []
    port = model(serve(OK, seen=seen))
    stopped = (await collect(port))[-1]
    assert isinstance(stopped, Stopped)
    native = {
        "role": "assistant",
        "content": "Hi",
        "tool_calls": [
            {
                "id": "call_v",
                "type": "function",
                "function": {"name": "view", "arguments": '{"path": "main.tex"}'},
            }
        ],
    }
    assert stopped.message.native == native
    assert stopped.message.blocks == (
        TextBlock("Hi"),
        ToolUseBlock("call_v", "view", {"path": "main.tex"}),
    )
    results = Message(
        "user", (TextBlock("Stop after this."), ToolResultBlock("call_v", "1| hello", True))
    )
    built = Message(
        "assistant", (TextBlock("Looking."), ToolUseBlock("call_h", "view", {"path": "a.tex"}))
    )
    await collect(port, make_call(user("hi"), stopped.message, results, built))
    body = bodies_of(seen)[1]
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        native,  # replayed exactly as received, argument string included
        {"role": "tool", "tool_call_id": "call_v", "content": "1| hello"},
        {"role": "user", "content": "Stop after this."},
        {
            "role": "assistant",
            "content": "Looking.",
            "tool_calls": [
                {
                    "id": "call_h",
                    "type": "function",
                    "function": {"name": "view", "arguments": '{"path": "a.tex"}'},
                }
            ],
        },
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "view",
                "description": "View numbered lines.",
                "parameters": VIEW_SCHEMA,
            },
        }
    ]


# -- streamed replies ------------------------------------------------------------


@pytest.mark.anyio
async def test_openai_compat_length_stop_marks_the_cut_off_tool_call_truncated() -> None:
    body = sse(
        chunk(fn(0, '{"path": "a.tex"}', id="call_1", name="view")),
        chunk(fn(1, '{"path": "b.t', id="call_2", name="view")),
        chunk(finish="length"),
        chunk(usage=USAGE),
    )
    events = await collect(model(serve(body)))
    calls = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert [(c.id, c.args, c.parse_error) for c in calls] == [
        ("call_1", {"path": "a.tex"}, None),
        ("call_2", None, "truncated"),
    ]
    assert calls[1].raw_args == '{"path": "b.t'
    assert isinstance(events[-1], Stopped) and events[-1].reason == "max_tokens"


@pytest.mark.anyio
async def test_openai_compat_tool_calls_without_ids_and_a_stop_finish_are_tool_use() -> None:
    single_escaped = '{"path": "x\\section"}'  # the model wrote \section unescaped
    body = sse(
        chunk({"tool_calls": [{"index": 0, "function": {"name": "check", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 1, "function": {"name": "view"}}]}),
        chunk({"tool_calls": [{"index": 1, "function": {"arguments": single_escaped}}]}),
        chunk(finish="stop"),
    )
    events = await collect(model(serve(body), "lmstudio", key=None))
    calls = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert [(c.id, c.name, c.raw_args) for c in calls] == [
        ("call_1", "check", "{}"),
        ("call_2", "view", single_escaped),
    ]
    assert calls[1].args == {"path": "x\\section"}
    assert isinstance(events[-1], Stopped) and events[-1].reason == "tool_use"
    assert not any(isinstance(e, UsageReport) for e in events)  # the server sent no usage


@pytest.mark.anyio
async def test_openai_compat_reasoning_announces_thinking_and_refusal_text_is_a_refusal() -> None:
    body = sse(
        chunk({"role": "assistant", "reasoning_content": "Let me"}),
        chunk({"reasoning_content": " think."}),
        chunk({"content": "Done."}),
        chunk(finish="stop"),
    )
    events = await collect(model(serve(body), "vllm"))
    assert [type(e) for e in events] == [ThinkingStarted, TextDelta, Stopped]
    stopped = events[-1]
    assert isinstance(stopped, Stopped) and stopped.reason == "end"
    assert stopped.message.native == {"role": "assistant", "content": "Done."}

    refused = sse(
        chunk({"role": "assistant", "refusal": "I can't help with that."}), chunk(finish="stop")
    )
    events = await collect(model(serve(refused)))
    assert not any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Stopped) and events[-1].reason == "refusal"


@pytest.mark.anyio
async def test_openai_compat_stream_that_never_finishes_is_an_error() -> None:
    body = sse(chunk({"role": "assistant", "content": "Half"}))
    with pytest.raises(ProviderError) as caught:
        await collect(model(serve(body)))
    assert caught.value.code == "unreachable"


@pytest.mark.anyio
async def test_openai_compat_error_bodies_map_to_provider_errors_without_the_key() -> None:
    too_long = "This model's maximum context length is 32768 tokens. You requested 40000."
    vllm_error = {"object": "error", "message": too_long, "type": "BadRequestError", "code": 400}
    stream_error = 'data: {"error": {"message": "The server had an error"}}\n\n'
    cases: list[tuple[httpx.Response | bytes, str, bool]] = [
        (
            api_error(400, {"error": {"message": too_long, "code": "context_length_exceeded"}}),
            "context_overflow",
            False,
        ),
        (api_error(400, vllm_error), "context_overflow", False),
        (
            api_error(429, {"error": {"message": "Quota", "code": "insufficient_quota"}}),
            "rate_limited",
            False,
        ),
        (
            api_error(402, {"error": {"message": "Insufficient credits", "code": 402}}),
            "rate_limited",
            False,
        ),
        (sse(chunk({"content": "Hi"}), stream_error), "overloaded", True),
        (sse("data: {not json}\n\n"), "bad_request", False),
    ]
    for reply, code, retryable in cases:
        with pytest.raises(ProviderError) as caught:
            await collect(model(serve(reply)))
        assert (caught.value.code, caught.value.retryable) == (code, retryable), code

    secret = "sk-test-0000"
    echoed = api_error(400, {"error": {"message": f"Invalid 'tools' for key {secret}"}})
    with pytest.raises(ProviderError) as caught:
        await collect(model(serve(echoed)))
    assert caught.value.code == "bad_request"
    assert "Invalid 'tools'" in caught.value.message and secret not in caught.value.message
    assert "[redacted]" in caught.value.message


def test_openai_compat_model_refuses_a_bad_setup_and_never_shows_the_key(tmp_path: Path) -> None:
    seen: Requests = []
    handler = serve(OK, seen=seen)
    with pytest.raises(ValueError) as caught:
        model(handler, ledger=None)
    assert str(caught.value) == (
        "a cost ledger is required for paid calls; set PRISMTEX_LEDGER_PATH"
    )
    with pytest.raises(ValueError, match="PRISMTEX_LEDGER_PATH"):
        model(handler, "vllm", ledger=None, paid=True)  # a priced endpoint is paid too
    model(handler, "vllm", ledger=None)  # a free endpoint needs no ledger
    with pytest.raises(ValueError, match="PRISMTEX_OPENAI_API_KEY"):
        model(handler, "openrouter", key=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="not an OpenAI-compatible model"):
        OpenAICompatModel(
            spec("anthropic:claude-haiku-5-5"),
            preset=PRESETS["openai"],
            base_url=BASE["openai"],
            api_key="k",
            http_client=client,
            ledger_path=tmp_path / "l",
        )
    with pytest.raises(ValueError, match="not a model of the openai preset"):
        OpenAICompatModel(
            spec("vllm:qwen"),
            preset=PRESETS["openai"],
            base_url=BASE["openai"],
            api_key="k",
            http_client=client,
            ledger_path=tmp_path / "l",
        )
    with pytest.raises(ValueError, match="PRISMTEX_OPENAI_BASE_URL"):
        OpenAICompatModel(
            spec("vllm:qwen"),
            preset=PRESETS["vllm"],
            base_url="ftp://x",
            api_key=None,
            http_client=client,
        )
    assert seen == []
    port = model(handler)
    assert "sk-test-0000" not in repr(port) and "openai:m-1" in repr(port)
    assert port.ledger_path == Path("ledger.jsonl") and port.paid


# -- the shared adapter contract -------------------------------------------------


class TestOpenAICompatContract(ModelPortContract):
    max_output_path = ("max_completion_tokens",)
    forbidden_fields = frozenset({"temperature", "top_p", "top_k", "tool_choice"})

    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path: Path) -> Iterator[None]:
        self.ledger = tmp_path / "ledger.jsonl"
        yield
        assert not self.ledger.exists()  # the adapter never writes the ledger itself

    def make_port(self, handler: Handler) -> ModelPort:
        return OpenAICompatModel(
            spec("openai:gpt-test"),
            preset=PRESETS["openai"],
            base_url=BASE["openai"],
            api_key=self.secret,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ledger_path=self.ledger,
        )

    def ok_response(self) -> httpx.Response:
        return streamed(OK)

    def text_prefix(self) -> bytes:
        return chunk({"role": "assistant", "content": "Hi"}).encode()

    def error_response(self, kind: str) -> httpx.Response | Exception:
        if kind == "unreachable":
            return httpx.ConnectError("connection refused")
        if kind == "timeout":
            return httpx.ReadTimeout("timed out")
        status = {"auth": 401, "not_found": 404, "rate_limited": 429, "overloaded": 503}[kind]
        body = {"error": {"message": f"{kind} for test", "type": "error", "code": None}}
        return api_error(status, body, **{"retry-after": "0"})
