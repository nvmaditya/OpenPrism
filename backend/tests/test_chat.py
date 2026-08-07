"""Phase 7: SSE chat stream over LangGraph agent (scripted model)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agent.scripted import ScriptedChatModel
from app.agent.stream import iter_agent_events, sse_frame
from app.main import create_app
from app.settings import Settings


def _parse_sse(text: str) -> list[dict]:
    """Parse SSE text into list of {event, data}."""
    events: list[dict] = []
    current_event = "message"
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                events.append({"event": current_event, "data": payload})
            current_event = "message"
            data_lines = []
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        events.append({"event": current_event, "data": payload})
    return events


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def app_client(projects_root: Path):
    app = create_app(
        Settings(
            prism_projects_root=projects_root,
            prism_preview_root=projects_root.parent / "previews",
            prism_skill_home=projects_root.parent / "skill-home",
        )
    )
    client = TestClient(app)
    return app, client, projects_root


def test_sse_frame_format():
    frame = sse_frame("token", {"text": "hi"})
    assert frame.startswith("event: token\n")
    assert 'data: {"text": "hi"}' in frame
    assert frame.endswith("\n\n")


def test_chat_stream_token_and_done(app_client):
    app, client, _root = app_client
    pid = client.post("/api/projects", json={"kind": "blank", "name": "Chat"}).json()[
        "id"
    ]
    app.state.chat_model_override = ScriptedChatModel(
        script=[AIMessage(content="Hello from Prism agent.")]
    )

    with client.stream(
        "POST",
        f"/api/projects/{pid}/chat",
        json={"message": "Say hello"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    names = [e["event"] for e in events]
    assert "token" in names
    assert "done" in names
    assert names[-1] == "done"
    tokens = [e["data"].get("text", "") for e in events if e["event"] == "token"]
    assert any("Hello" in t or "Prism" in t for t in tokens)
    done = events[-1]["data"]
    assert done.get("ok") is True


def test_chat_stream_file_write_on_disk(app_client):
    app, client, projects_root = app_client
    pid = client.post("/api/projects", json={"kind": "blank", "name": "Write"}).json()[
        "id"
    ]
    secret = "CHAT_STREAM_SECRET_77"
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_project_file",
                        "args": {"path": "chat-out.txt", "content": secret},
                        "id": "call_w1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=f"Wrote chat-out.txt with {secret}"),
        ]
    )

    with client.stream(
        "POST",
        f"/api/projects/{pid}/chat",
        json={"message": "Write chat-out.txt"},
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    events = _parse_sse(body)
    names = [e["event"] for e in events]
    assert "file_changed" in names
    assert "done" in names
    assert "tool_start" in names or "tool_end" in names

    fc = next(e for e in events if e["event"] == "file_changed")
    assert fc["data"]["path"] == "chat-out.txt"
    assert fc["data"]["op"] == "write"

    on_disk = projects_root / pid / "chat-out.txt"
    assert on_disk.is_file()
    assert on_disk.read_text(encoding="utf-8") == secret

    # isolation: only this project
    for child in projects_root.iterdir():
        if child.is_dir() and child.name != pid:
            assert not (child / "chat-out.txt").exists()


def test_chat_missing_project_404(app_client):
    _app, client, _root = app_client
    r = client.post(
        f"/api/projects/{'0' * 32}/chat",
        json={"message": "hi"},
    )
    assert r.status_code == 404


def test_iter_agent_events_direct(projects_root: Path):
    from app.domain.project import ProjectStore

    store = ProjectStore(projects_root)
    proj = store.create(kind="blank", name="E")
    model = ScriptedChatModel(script=[AIMessage(content="direct")])
    events = list(
        iter_agent_events(store, proj.id, "hi", model=model)
    )
    assert events[-1][0] == "done"
    assert any(e[0] == "token" for e in events)


def test_health(app_client):
    _app, client, _ = app_client
    assert client.get("/health").status_code == 200
