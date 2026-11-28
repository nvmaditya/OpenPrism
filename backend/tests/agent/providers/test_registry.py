"""The Provider registry: default model, paid-model checks, health, catalog and the agent
settings it reads (agent design §11.3, §11.6, §11.7), offline through httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from prismtex.agent.budget import Price
from prismtex.agent.providers import ModelEvent, ModelPort, ModelUnavailable, TextDelta
from prismtex.agent.providers.anthropic import AnthropicModel
from prismtex.agent.providers.catalog import (
    HTTP_TIMEOUT,
    OLLAMA_DEFAULT,
    PRESETS,
    ProviderRegistry,
    spec,
)
from prismtex.agent.providers.fake import FakeModel
from prismtex.agent.providers.ollama import OllamaModel
from prismtex.agent.providers.openai_compat import OpenAICompatModel
from prismtex.settings import Settings

Requests = list[httpx.Request]
QWEN = OLLAMA_DEFAULT.removeprefix("ollama:")
VLLM = {
    "openai_preset": "vllm",
    "openai_model": "qwen",
    "openai_base_url": "http://127.0.0.1:8001/v1",
}
CHAT = (
    'data: {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "qwen", '
    '"choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": "stop"}]}\n\n'
    "data: [DONE]\n\n"
)


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]


def local(pulled: list[str], seen: Requests, *, ollama_up: bool = True) -> httpx.AsyncClient:
    """A client over a fake loopback: Ollama with `pulled` models and a chat endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            if not ollama_up:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"models": [{"name": name} for name in pulled]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        if request.url.path.endswith("/chat/completions"):
            headers = {"content-type": "text/event-stream"}
            return httpx.Response(200, headers=headers, content=CHAT.encode())
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def collect(port: ModelPort) -> list[ModelEvent]:
    from prismtex.agent.providers import Message, ModelCall, TextBlock

    call = ModelCall(system="s", messages=(Message("user", (TextBlock("hi"),)),))
    with anyio.fail_after(30):
        return [event async for event in port.stream(call)]


def unavailable(registry: ProviderRegistry, ref: str | None) -> ModelUnavailable:
    with pytest.raises(ModelUnavailable) as caught:
        registry.resolve(ref, None)
    return caught.value


# -- the default model -----------------------------------------------------------


@pytest.mark.anyio
async def test_default_model_resolution_order(tmp_path: Path) -> None:
    seen: Requests = []
    client = local([QWEN], seen)
    ledger = tmp_path / "ledger.jsonl"
    everything = settings(anthropic_api_key="sk-ant-x", **VLLM)
    registry = ProviderRegistry(everything, ledger_path=ledger, http_client=client)

    # The Turn's ref, then the Project setting, then Settings.default_model.
    haiku = registry.resolve("anthropic:claude-haiku-5-5", "vllm:qwen")
    assert isinstance(haiku, AnthropicModel) and haiku.spec.ref == "anthropic:claude-haiku-5-5"
    endpoint = registry.resolve(None, "vllm:qwen")
    assert isinstance(endpoint, OpenAICompatModel) and endpoint.spec.ref == "vllm:qwen"
    configured = settings(anthropic_api_key="sk-ant-x", default_model="vllm:qwen", **VLLM)
    with_default = ProviderRegistry(configured, ledger_path=ledger, http_client=client)
    assert with_default.resolve(None, None).spec.ref == "vllm:qwen"
    assert with_default.resolve(None, "anthropic:claude-sonnet-5-5").spec.ref == (
        "anthropic:claude-sonnet-5-5"
    )

    # Then the first available: Ollama's default only once a probe saw it pulled ...
    assert registry.resolve(None, None).spec.ref == "anthropic:claude-opus-5-5"
    assert (await registry.health()).default_model == "anthropic:claude-opus-5-5"
    await registry.health(probe=True)
    ollama = registry.resolve(None, None)
    assert isinstance(ollama, OllamaModel) and ollama.spec.ref == OLLAMA_DEFAULT
    assert (await registry.health()).default_model == OLLAMA_DEFAULT
    # ... then Claude Opus 5.5 when a key is set, then the configured endpoint.
    no_key = ProviderRegistry(settings(**VLLM), ledger_path=ledger, http_client=client)
    assert no_key.resolve(None, None).spec.ref == "vllm:qwen"

    # Nothing available: setup instructions naming the variables.
    nothing = ProviderRegistry(settings(), ledger_path=ledger, http_client=client)
    error = unavailable(nothing, None)
    assert (error.code, error.env_var) == ("model_unavailable", "PRISMTEX_DEFAULT_MODEL")
    for hint in (
        f"ollama pull {QWEN}",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "PRISMTEX_OPENAI_PRESET",
        "PRISMTEX_OPENAI_MODEL",
        "PRISMTEX_DEFAULT_MODEL",
    ):
        assert hint in error.message

    # Refs nobody serves are unknown; there is no Groq preset and no PRISMTEX_PROVIDER.
    for ref in ("groq:llama-3.3-70b", "fake:m", "fake:scripted", "anthropic:claude-2", "x"):
        assert unavailable(registry, ref).code == "unknown_model", ref
    assert "provider" not in Settings.model_fields
    assert {request.url.path for request in seen} <= {"/api/tags", "/api/show"}


@pytest.mark.anyio
async def test_ollama_settings_reach_the_ollama_adapter(tmp_path: Path) -> None:
    seen: Requests = []
    client = local([QWEN, "mistral-nemo:latest"], seen)
    pinned = settings(ollama_url="http://127.0.0.1:11500/", ollama_num_ctx=12_288)
    registry = ProviderRegistry(pinned, ledger_path=None, http_client=client)
    port = registry.resolve(OLLAMA_DEFAULT, None)
    assert isinstance(port, OllamaModel)
    assert (port.spec.context_window, port.spec.num_ctx) == (12_288, 12_288)
    # An uncatalogued model reads its tool support from the last probe.
    assert not registry.resolve("ollama:mistral-nemo", None).spec.native_tools
    await registry.health(probe=True)
    assert registry.resolve("ollama:mistral-nemo", None).spec.native_tools
    assert {(r.url.host, r.url.port) for r in seen} == {("127.0.0.1", 11500)}

    broken = ProviderRegistry(settings(ollama_url="localhost:11434"), ledger_path=None)
    error = unavailable(broken, OLLAMA_DEFAULT)
    assert (error.code, error.env_var) == ("model_unavailable", "PRISMTEX_OLLAMA_URL")
    report = await broken.health(probe=True)  # no request: the URL is not http(s)
    assert [issue.code for issue in report.issues][:1] == ["ollama_unreachable"]
    await broken.aclose()


# -- paid models -----------------------------------------------------------------


def test_paid_model_without_a_price_is_unavailable_naming_prismtex_openai_price(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    seen: Requests = []
    client = local([], seen)
    for preset in ("openai", "openrouter"):
        configured = settings(openai_preset=preset, openai_model="m", openai_api_key="sk-o")
        registry = ProviderRegistry(configured, ledger_path=ledger, http_client=client)
        error = unavailable(registry, f"{preset}:m")
        assert (error.code, error.env_var) == ("model_unavailable", "PRISMTEX_OPENAI_PRICE")
        assert "PRISMTEX_OPENAI_PRICE" in error.message
        [info] = [info for info in registry.catalog() if info.ref == f"{preset}:m"]
        assert info.paid and info.price is None and not info.available

    priced = settings(
        openai_preset="openai", openai_model="m", openai_api_key="sk-o", openai_price="0.4,1.6"
    )
    port = ProviderRegistry(priced, ledger_path=ledger, http_client=client).resolve(
        "openai:m", None
    )
    assert isinstance(port, OpenAICompatModel) and port.spec.paid
    # Cached input is charged at the full input rate: the endpoint's cache rate is unknown.
    assert port.spec.price == Price(input=0.4, output=1.6, cache_write=0.4, cache_read=0.4)

    # A price also meters an endpoint behind a free preset.
    metered = ProviderRegistry(settings(openai_price=(0.1, 0.2), **VLLM), ledger_path=ledger)
    assert metered.resolve("vllm:qwen", None).spec.paid

    # A preset that needs a key refuses to run without one.
    keyless = settings(openai_preset="openai", openai_model="m", openai_price="1,2")
    error = unavailable(ProviderRegistry(keyless, ledger_path=ledger), "openai:m")
    assert error.env_var == "PRISMTEX_OPENAI_API_KEY"
    assert seen == []


@pytest.mark.anyio
async def test_paid_model_without_a_ledger_is_unavailable_naming_prismtex_ledger_path(
    tmp_path: Path,
) -> None:
    seen: Requests = []
    client = local([], seen)
    paid = settings(
        anthropic_api_key="sk-ant-x",
        openai_preset="openai",
        openai_model="m",
        openai_api_key="sk-o",
        openai_price="1,2",
    )
    registry = ProviderRegistry(paid, ledger_path=None, http_client=client)
    for ref in ("anthropic:claude-opus-5-5", "anthropic:claude-haiku-5-5", "openai:m"):
        error = unavailable(registry, ref)
        assert (error.code, error.env_var) == ("model_unavailable", "PRISMTEX_LEDGER_PATH"), ref
        assert "PRISMTEX_LEDGER_PATH" in error.message
    metered = ProviderRegistry(settings(openai_price="1,2", **VLLM), ledger_path=None)
    assert unavailable(metered, "vllm:qwen").env_var == "PRISMTEX_LEDGER_PATH"
    free = ProviderRegistry(settings(**VLLM), ledger_path=None, http_client=client)
    assert isinstance(free.resolve("vllm:qwen", None), OpenAICompatModel)
    report = await registry.health()
    assert not report.ok and report.default_model == "anthropic:claude-opus-5-5"
    assert seen == []

    # from_env supplies the ledger: the configured path, else <data_dir>/state/ledger.jsonl,
    # and an explicit path (eval runs) overrides both.
    keyed = settings(data_dir=tmp_path, anthropic_api_key="sk-ant-x")
    port = ProviderRegistry.from_env(keyed, http_client=client).resolve(None, None)
    assert isinstance(port, AnthropicModel)
    assert port.ledger_path == tmp_path / "state" / "ledger.jsonl"
    kept = settings(
        data_dir=tmp_path, anthropic_api_key="sk-ant-x", ledger_path=tmp_path / "kept.jsonl"
    )
    port = ProviderRegistry.from_env(kept, http_client=client).resolve(None, None)
    assert isinstance(port, AnthropicModel) and port.ledger_path == tmp_path / "kept.jsonl"
    override = tmp_path / "eval.jsonl"
    port = ProviderRegistry.from_env(kept, ledger_path=override, http_client=client).resolve(
        None, None
    )
    assert isinstance(port, AnthropicModel) and port.ledger_path == override
    assert not port.fallbacks
    with_fallbacks = settings(
        data_dir=tmp_path, anthropic_api_key="sk-ant-x", anthropic_fallbacks="default"
    )
    port = ProviderRegistry.from_env(with_fallbacks, http_client=client).resolve(None, None)
    assert isinstance(port, AnthropicModel) and port.fallbacks
    assert not (tmp_path / "state").exists()  # resolving never writes the ledger


# -- health and catalog ----------------------------------------------------------


@pytest.mark.anyio
async def test_health_without_probe_and_catalog_make_no_network_request(tmp_path: Path) -> None:
    seen: Requests = []
    client = local([QWEN, "mistral-nemo:latest"], seen)
    configured = settings(
        anthropic_api_key="sk-ant-secret",
        openai_preset="openai",
        openai_model="gpt-x",
        openai_api_key="sk-o-secret",
        openai_price="1,2",
    )
    registry = ProviderRegistry(configured, ledger_path=tmp_path / "l.jsonl", http_client=client)
    report = await registry.health()
    listed = registry.catalog()
    assert seen == []
    assert report.ok and report.default_model == "anthropic:claude-opus-5-5"
    by_name = {provider.provider: provider for provider in report.providers}
    assert set(by_name) == {"ollama", "anthropic", "openai_compatible"}
    assert by_name["ollama"].reachable is None and by_name["ollama"].configured
    assert by_name["anthropic"].configured and by_name["anthropic"].reachable is None
    assert by_name["openai_compatible"].configured
    assert by_name["openai_compatible"].detail == "openai:gpt-x at https://api.openai.com/v1."
    assert not any(info.available for info in listed if info.provider == "ollama")

    probed = await registry.health(probe=True)
    assert seen and all((r.url.host, r.url.port) == ("127.0.0.1", 11434) for r in seen)
    assert [r.url.path for r in seen] == ["/api/tags", "/api/show"]
    assert json.loads(seen[1].content) == {"model": "mistral-nemo"}  # once, for its bare name
    assert probed.ok and probed.default_model == OLLAMA_DEFAULT and probed.issues == ()
    assert {p.provider: p.reachable for p in probed.providers}["ollama"] is True

    infos = {info.ref: info for info in registry.catalog()}
    assert list(infos) == [
        OLLAMA_DEFAULT,
        "ollama:llama3.1:8b",
        "ollama:mistral-nemo",
        "anthropic:claude-haiku-5-5",
        "anthropic:claude-sonnet-5-5",
        "anthropic:claude-opus-5-5",
        "openai:gpt-x",
    ]
    assert infos[OLLAMA_DEFAULT].available and not infos["ollama:llama3.1:8b"].available
    assert infos["ollama:mistral-nemo"].native_tools and infos["ollama:mistral-nemo"].available
    assert all(infos[ref].available for ref in infos if ref.startswith("anthropic:"))
    assert infos["openai:gpt-x"].available and infos["openai:gpt-x"].context_window == 32_768
    assert len(seen) == 2  # catalog and health after the probe made no request

    shown = repr(report) + repr(probed) + repr(registry.catalog()) + repr(registry)
    assert "sk-ant-secret" not in shown and "sk-o-secret" not in shown


@pytest.mark.anyio
async def test_health_issues_say_what_to_fix() -> None:
    seen: Requests = []
    down = ProviderRegistry(
        settings(), ledger_path=None, http_client=local([], seen, ollama_up=False)
    )
    report = await down.health(probe=True)
    assert not report.ok and report.default_model is None
    assert [issue.code for issue in report.issues] == [
        "ollama_unreachable",
        "anthropic_key_missing",
    ]
    assert "PRISMTEX_OLLAMA_URL" in report.issues[0].message
    assert "ANTHROPIC_API_KEY" in report.issues[1].message
    assert {p.provider: p.reachable for p in report.providers}["ollama"] is False

    missing = ProviderRegistry(
        settings(), ledger_path=None, http_client=local(["llama3.1:8b"], seen)
    )
    report = await missing.health(probe=True)
    assert report.issues[0].code == "ollama_model_missing"
    assert f"Run: ollama pull {QWEN}" in report.issues[0].message

    for base_url in (None, "127.0.0.1:8001"):
        endpoint = settings(openai_preset="vllm", openai_model="q", openai_base_url=base_url)
        report = await ProviderRegistry(endpoint, ledger_path=None).health()
        assert "openai_base_url_missing" in [issue.code for issue in report.issues]
        detail = {p.provider: p.detail for p in report.providers}["openai_compatible"]
        assert detail is not None and "PRISMTEX_OPENAI_BASE_URL" in detail
    unpriced = settings(openai_preset="openrouter", openai_model="v/m", openai_api_key="sk-r")
    report = await ProviderRegistry(unpriced, ledger_path=None).health()
    assert [issue.code for issue in report.issues] == ["price_missing"]
    assert not report.ok and report.default_model == "openrouter:v/m"

    claude_default = settings(default_model="anthropic:claude-opus-5-5")
    report = await ProviderRegistry(claude_default, ledger_path=None).health()
    assert not report.ok and [issue.code for issue in report.issues] == ["anthropic_key_missing"]
    assert {request.url.path for request in seen} == {"/api/tags"}


# -- settings the registry reads -------------------------------------------------


@pytest.mark.anyio
async def test_claude_api_key_alias_is_accepted_and_lmstudio_has_the_text_recovery_quirk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-claude")
    from_alias = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert from_alias.anthropic_api_key is not None
    assert from_alias.anthropic_api_key.get_secret_value() == "sk-ant-claude"
    assert "sk-ant-claude" not in repr(from_alias)
    registry = ProviderRegistry(from_alias, ledger_path=Path("ledger.jsonl"))
    assert registry.resolve(None, None).spec.ref == "anthropic:claude-opus-5-5"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-first")
    first = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert first.anthropic_api_key is not None
    assert first.anthropic_api_key.get_secret_value() == "sk-ant-first"
    direct = settings(anthropic_api_key="sk-ant-direct").anthropic_api_key
    assert direct is not None and direct.get_secret_value() == "sk-ant-direct"

    monkeypatch.delenv("CLAUDE_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    assert PRESETS["lmstudio"].quirks == {"recover_text_tool_calls"}
    assert spec("lmstudio:qwen3-8b").quirks == {"recover_text_tool_calls"}
    seen: Requests = []
    client = local([], seen)
    studio = settings(openai_preset="lmstudio", openai_model="qwen3-8b")
    port = ProviderRegistry(studio, ledger_path=None, http_client=client).resolve(None, None)
    assert isinstance(port, OpenAICompatModel)
    assert port.spec.quirks == {"recover_text_tool_calls"} and not port.spec.paid
    assert port.base_url == "http://127.0.0.1:1234/v1"
    await collect(port)
    assert str(seen[-1].url) == "http://127.0.0.1:1234/v1/chat/completions"
    moved = settings(
        openai_preset="lmstudio",
        openai_model="qwen3-8b",
        openai_base_url="http://127.0.0.1:4321/v1",
    )
    port = ProviderRegistry(moved, ledger_path=None, http_client=client).resolve(None, None)
    assert isinstance(port, OpenAICompatModel) and port.base_url == "http://127.0.0.1:4321/v1"


def test_agent_settings_are_read_from_a_dotenv_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "PRISMTEX_DEFAULT_MODEL=ollama:qwen3:4b-instruct-2507-q4_K_M\n"
        "CLAUDE_API_KEY=sk-ant-dotenv\n"
        "PRISMTEX_OPENAI_PRESET=vllm\n"
        "PRISMTEX_OPENAI_MODEL=qwen\n"
        "PRISMTEX_OPENAI_BASE_URL=http://127.0.0.1:8001/v1\n"
        "PRISMTEX_OPENAI_API_KEY=\n"
        "PRISMTEX_OPENAI_PRICE=0.5,1.5\n"
        "PRISMTEX_TRACE_PROMPTS=1\n"
        "PRISMTEX_MAX_USD_PER_TURN=0.25\n"
        "PRISMTEX_COST_STOP_USD=\n",
        encoding="utf-8",
    )
    loaded = Settings(_env_file=env)  # pyright: ignore[reportCallIssue]
    assert loaded.default_model == "ollama:qwen3:4b-instruct-2507-q4_K_M"
    assert loaded.anthropic_api_key is not None
    assert loaded.anthropic_api_key.get_secret_value() == "sk-ant-dotenv"
    assert (loaded.openai_preset, loaded.openai_model) == ("vllm", "qwen")
    assert loaded.openai_base_url == "http://127.0.0.1:8001/v1"
    assert loaded.openai_api_key is None  # a blank key is no key
    assert loaded.openai_price == (0.5, 1.5)
    assert loaded.trace_prompts and loaded.max_usd_per_turn == 0.25
    assert loaded.cost_stop_usd is None
    registry = ProviderRegistry(loaded, ledger_path=None)
    assert registry.resolve(None, None).spec.ref == OLLAMA_DEFAULT


@pytest.mark.anyio
async def test_fake_scripted_is_listed_only_when_settings_name_a_script(tmp_path: Path) -> None:
    plain = ProviderRegistry(settings(), ledger_path=None)
    assert "fake:scripted" not in [info.ref for info in plain.catalog()]
    assert unavailable(plain, "fake:scripted").code == "unknown_model"
    assert "fake" not in {p.provider for p in (await plain.health()).providers}

    script = tmp_path / "script.json"
    script.write_text(json.dumps({"loop": True, "steps": [{"say": "Hello."}]}), encoding="utf-8")
    scripted = ProviderRegistry(
        settings(fake_script=script, default_model="fake:scripted"), ledger_path=None
    )
    [info] = [info for info in scripted.catalog() if info.ref == "fake:scripted"]
    assert info.available and info.provider == "fake"
    port = scripted.resolve(None, None)
    assert isinstance(port, FakeModel) and port.spec.ref == "fake:scripted"
    events = await collect(port)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello."
    report = await scripted.health()
    assert report.ok and report.default_model == "fake:scripted"
    assert {p.provider: p.configured for p in report.providers}["fake"]

    gone = ProviderRegistry(settings(fake_script=tmp_path / "missing.json"), ledger_path=None)
    assert "fake:scripted" not in [info.ref for info in gone.catalog()]
    error = unavailable(gone, "fake:scripted")
    assert (error.code, error.env_var) == ("model_unavailable", "PRISMTEX_FAKE_SCRIPT")
    for registry in (plain, scripted, gone):
        await registry.aclose()


@pytest.mark.anyio
async def test_registry_http_client_uses_the_design_timeouts(tmp_path: Path) -> None:
    assert (HTTP_TIMEOUT.connect, HTTP_TIMEOUT.read) == (10.0, 120.0)
    owned = ProviderRegistry.from_env(settings(data_dir=tmp_path))
    assert owned._http.timeout == HTTP_TIMEOUT  # pyright: ignore[reportPrivateUsage]
    await owned.aclose()
    assert owned._http.is_closed  # pyright: ignore[reportPrivateUsage]

    seen: Requests = []
    lent = local([], seen)
    borrowed = ProviderRegistry.from_env(settings(data_dir=tmp_path), http_client=lent)
    await borrowed.aclose()
    assert not lent.is_closed  # the caller's client is the caller's to close

    # The SDK adapters keep the client's timeouts instead of their own defaults.
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=headers, content=CHAT.encode())

    timed = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=HTTP_TIMEOUT)
    registry = ProviderRegistry(settings(**VLLM), ledger_path=None, http_client=timed)
    await collect(registry.resolve("vllm:qwen", None))
    assert seen[-1].extensions["timeout"] == {
        "connect": 10.0,
        "read": 120.0,
        "write": 120.0,
        "pool": 120.0,
    }
