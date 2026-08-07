"""Phase 3: provider factory + LangGraph agent FS tools (mock LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from fastapi.testclient import TestClient

from app.agent.graph import last_ai_text, run_agent
from app.agent.providers import (
    ProviderError,
    SUPPORTED_PROVIDERS,
    describe_chat_model,
    get_chat_model,
)
from app.agent.scripted import ScriptedChatModel
from app.domain.project import ProjectStore
from app.main import create_app
from app.settings import Settings


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def store(projects_root: Path) -> ProjectStore:
    return ProjectStore(projects_root)


def test_factory_openai_vs_ollama_distinct():
    openai = get_chat_model(
        Settings(
            prism_llm_provider="openai",
            prism_llm_model="gpt-4o-mini",
            prism_llm_api_key="sk-test-openai",
            prism_llm_base_url="",
        )
    )
    ollama = get_chat_model(
        Settings(
            prism_llm_provider="ollama",
            prism_llm_model="llama3.2",
            prism_llm_api_key="ollama",
            prism_llm_base_url="http://127.0.0.1:11434/v1",
        )
    )
    d_oai = describe_chat_model(openai)
    d_oll = describe_chat_model(ollama)

    assert d_oai["provider"] == "openai"
    assert d_oll["provider"] == "ollama"
    assert d_oai["class"] == d_oll["class"]  # both ChatOpenAI-compatible
    assert d_oai["model"] == "gpt-4o-mini"
    assert d_oll["model"] == "llama3.2"
    # Distinct backends via base_url on ollama
    assert "11434" in (d_oll.get("base_url") or "")
    # openai default has no custom ollama base
    assert "11434" not in (d_oai.get("base_url") or "")


def test_factory_azure_and_anthropic_and_custom():
    azure = get_chat_model(
        Settings(
            prism_llm_provider="azure",
            prism_llm_model="gpt-4o",
            prism_llm_api_key="az-key",
            prism_llm_base_url="https://myres.openai.azure.com/",
        )
    )
    d_az = describe_chat_model(azure)
    assert d_az["provider"] == "azure"
    assert d_az["class"] == "AzureChatOpenAI"
    assert "azure" in (d_az.get("base_url") or "").lower() or "myres" in (
        d_az.get("base_url") or ""
    )

    anth = get_chat_model(
        Settings(
            prism_llm_provider="anthropic",
            prism_llm_model="claude-3-5-sonnet-latest",
            prism_llm_api_key="anth-key",
        )
    )
    d_an = describe_chat_model(anth)
    assert d_an["provider"] == "anthropic"
    assert "Anthropic" in d_an["class"] or d_an["class"] == "ChatAnthropic"

    custom = get_chat_model(
        Settings(
            prism_llm_provider="custom",
            prism_llm_model="local-model",
            prism_llm_api_key="x",
            prism_llm_base_url="http://127.0.0.1:9000/v1",
        )
    )
    d_c = describe_chat_model(custom)
    assert d_c["provider"] == "custom"
    assert "9000" in (d_c.get("base_url") or "")


def test_factory_rejects_unknown_provider():
    with pytest.raises(ProviderError):
        get_chat_model(Settings(prism_llm_provider="not-a-provider"))


def test_supported_providers_documented():
    assert "openai" in SUPPORTED_PROVIDERS
    assert "ollama" in SUPPORTED_PROVIDERS
    assert "azure" in SUPPORTED_PROVIDERS
    assert "anthropic" in SUPPORTED_PROVIDERS
    assert "custom" in SUPPORTED_PROVIDERS
    assert "groq" in SUPPORTED_PROVIDERS


def test_factory_groq_uses_key_and_base():
    m = get_chat_model(
        Settings(
            prism_llm_provider="groq",
            prism_llm_model="llama-3.3-70b-versatile",
            groq_api_key="gsk_test_key",
            prism_llm_api_key="",
            prism_llm_base_url="",
        )
    )
    d = describe_chat_model(m)
    assert d["provider"] == "groq"
    assert "groq.com" in (d.get("base_url") or "")
    assert d.get("model") == "llama-3.3-70b-versatile"


def test_agent_write_then_read_with_scripted_model(store: ProjectStore, projects_root: Path):
    project = store.create(kind="blank", name="AgentProj")
    pid = project.id
    secret = "AGENT_SECRET_PAYLOAD_99"

    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_project_file",
                    "args": {"path": "agent-out.txt", "content": secret},
                    "id": "call_write_1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_project_file",
                    "args": {"path": "agent-out.txt"},
                    "id": "call_read_1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=f"Done; file contains {secret}"),
    ]
    model = ScriptedChatModel(script=script)

    result = run_agent(
        store,
        pid,
        user_message="Write agent-out.txt then read it back",
        model=model,
    )

    on_disk = projects_root / pid / "agent-out.txt"
    assert on_disk.is_file(), "agent write must land on real project disk"
    assert on_disk.read_text(encoding="utf-8") == secret

    # Only under this project
    assert list(projects_root.iterdir())  # at least one project dir
    for child in projects_root.iterdir():
        if child.name != pid and child.is_dir():
            assert not (child / "agent-out.txt").exists()

    text = last_ai_text(result)
    assert secret in text or "Done" in text


def test_agent_tool_respects_path_escape(store: ProjectStore, projects_root: Path):
    project = store.create(kind="blank", name="Escape")
    pid = project.id
    outside = projects_root.parent / "pwned.txt"
    if outside.exists():
        outside.unlink()

    script = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_project_file",
                    "args": {"path": "../pwned.txt", "content": "nope"},
                    "id": "call_bad",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="attempted escape"),
    ]
    result = run_agent(
        store,
        pid,
        user_message="try escape",
        model=ScriptedChatModel(script=script),
    )
    assert not outside.exists()
    # tool error should appear in messages
    msgs = result.get("messages") or []
    joined = " ".join(str(getattr(m, "content", "")) for m in msgs)
    assert "path_escape" in joined or "ERROR" in joined


def test_health_with_agent_module_loaded(projects_root: Path):
    client = TestClient(
        create_app(Settings(prism_projects_root=projects_root))
    )
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
