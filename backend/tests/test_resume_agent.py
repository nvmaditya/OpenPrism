"""Resume Ask-anything agent: structure-safe edits, verify gate, traces."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agent.scripted import ScriptedChatModel
from app.main import create_app
from app.settings import Settings

HAS_PDFLATEX = shutil.which("pdflatex") is not None


def _parse_sse(text: str) -> list[dict]:
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
    return app, TestClient(app), projects_root


def _chat(client: TestClient, pid: str, message: str) -> list[dict]:
    with client.stream(
        "POST",
        f"/api/projects/{pid}/chat",
        json={"message": message},
    ) as resp:
        assert resp.status_code == 200, resp.text
        body = "".join(resp.iter_text())
    return _parse_sse(body)


def _done(events: list[dict]) -> dict:
    assert events, "expected SSE events"
    assert events[-1]["event"] == "done"
    return events[-1]["data"]


def test_example_project_is_modular_resume(app_client):
    _app, client, projects_root = app_client
    example = client.post("/api/projects", json={"kind": "example"}).json()
    pid = example["id"]
    files = client.get(f"/api/projects/{pid}/files").json()["files"]
    assert "main.tex" in files
    assert any(p.startswith("sections/") and p.endswith(".tex") for p in files)
    main = client.get(f"/api/projects/{pid}/files/main.tex").json()["content"]
    assert r"\documentclass" in main
    assert r"\input{sections/" in main
    assert r"\begin{document}" in main
    assert r"\end{document}" in main
    # section files are fragments
    section = next(p for p in files if p.startswith("sections/") and p.endswith(".tex"))
    body = client.get(f"/api/projects/{pid}/files/{section}").json()["content"]
    assert r"\documentclass" not in body
    # blank stays a single main.tex
    blank = client.post("/api/projects", json={"kind": "blank"}).json()
    blank_files = client.get(f"/api/projects/{blank['id']}/files").json()["files"]
    assert "main.tex" in blank_files
    assert not any(p.startswith("sections/") for p in blank_files)
    assert (projects_root / pid / "sections").is_dir()


def test_chat_add_tex_input_keeps_preamble_and_existing_inputs(app_client):
    app, client, projects_root = app_client
    pid = client.post("/api/projects", json={"kind": "example"}).json()["id"]
    before = (
        client.get(f"/api/projects/{pid}/files/main.tex")
        .json()["content"]
        .replace("\r\n", "\n")
    )
    preamble = before.split(r"\begin{document}", 1)[0]
    existing_inputs = [
        line.strip() for line in before.splitlines() if r"\input{" in line
    ]
    phrase = "UNIQUEPROJECTSSECTIONZX9"
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_tex_input",
                        "args": {
                            "path": "sections/projects.tex",
                            "content": (
                                "\\section*{Projects}\n"
                                f"{phrase} built a compiler.\n"
                            ),
                        },
                        "id": "call_add_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Added sections/projects.tex"),
        ]
    )
    events = _chat(client, pid, "Add a Projects section as its own file")
    done = _done(events)
    assert done.get("trace_id"), done
    assert done.get("verified") is True, done
    assert done.get("rollback") is False
    on_disk = (projects_root / pid / "sections" / "projects.tex").read_text(
        encoding="utf-8"
    )
    assert phrase in on_disk
    after = (projects_root / pid / "main.tex").read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )
    assert after.split(r"\begin{document}", 1)[0] == preamble
    for line in existing_inputs:
        assert line in after
    assert r"\input{sections/projects}" in after
    assert r"\documentclass" in after


def test_chat_static_fail_rolls_back_and_writes_trace(app_client):
    app, client, projects_root = app_client
    pid = client.post("/api/projects", json={"kind": "blank"}).json()["id"]
    original = (projects_root / pid / "main.tex").read_text(encoding="utf-8")
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_project_file",
                        "args": {
                            "path": "main.tex",
                            "old_str": r"\end{document}",
                            "new_str": "",
                        },
                        "id": "call_break",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Removed end document"),
        ]
    )
    events = _chat(client, pid, "Delete the end of the document")
    done = _done(events)
    assert done.get("verified") is False, done
    assert done.get("rollback") is True, done
    trace_id = done.get("trace_id")
    assert isinstance(trace_id, str) and len(trace_id) == 32, done
    restored = (projects_root / pid / "main.tex").read_text(encoding="utf-8")
    assert restored == original
    assert r"\end{document}" in restored

    listed = client.get(f"/api/projects/{pid}/logs")
    assert listed.status_code == 200
    logs = listed.json()["logs"]
    assert any(item.get("log_id") == trace_id for item in logs)
    got = client.get(f"/api/projects/{pid}/logs/{trace_id}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body.get("kind") == "agent"
    assert body.get("verified") is False
    assert body.get("rollback") is True
    assert body.get("verify"), body
    assert "log" in body and body["log"]


def test_chat_structure_damage_rolls_back_headings(app_client):
    app, client, projects_root = app_client
    pid = client.post("/api/projects", json={"kind": "example"}).json()["id"]
    files = client.get(f"/api/projects/{pid}/files").json()["files"]
    section = next(p for p in files if p.startswith("sections/") and p.endswith(".tex"))
    original_section = (
        (projects_root / pid / section).read_bytes()
    )
    original_main = (projects_root / pid / "main.tex").read_bytes()
    # Wipe the section file so headings vanish
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_project_file",
                        "args": {"path": section, "content": "% emptied\n"},
                        "id": "call_wipe",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Cleared section"),
        ]
    )
    events = _chat(client, pid, "Clear that section")
    done = _done(events)
    assert done.get("verified") is False
    assert done.get("rollback") is True
    assert (projects_root / pid / section).read_bytes() == original_section
    assert (projects_root / pid / "main.tex").read_bytes() == original_main


def test_midturn_check_latex_still_runs_host_gate(app_client):
    app, client, _root = app_client
    pid = client.post("/api/projects", json={"kind": "blank"}).json()["id"]
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "check_latex",
                        "args": {"path": "main.tex"},
                        "id": "call_chk",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Checked main.tex"),
        ]
    )
    events = _chat(client, pid, "Check the latex")
    names = [
        e["data"].get("name")
        for e in events
        if e["event"] in ("tool_start", "tool_end")
    ]
    assert "check_latex" in names
    done = _done(events)
    assert done.get("trace_id")
    assert done.get("verified") is True


@pytest.mark.skipif(not HAS_PDFLATEX, reason="pdflatex not on PATH")
def test_chat_valid_edit_compiles_and_pdf_contains_phrase(app_client):
    app, client, projects_root = app_client
    pid = client.post("/api/projects", json={"kind": "example"}).json()["id"]
    phrase = "PDFPHRASEQK7M2"
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_tex_input",
                        "args": {
                            "path": "sections/awards.tex",
                            "content": f"\\section*{{Awards}}\n{phrase}\n",
                        },
                        "id": "call_awards",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Added awards"),
        ]
    )
    events = _chat(client, pid, f"Add an Awards section mentioning {phrase}")
    done = _done(events)
    assert done.get("verified") is True, done
    assert done.get("rollback") is False
    pdf = projects_root / pid / "main.pdf"
    assert pdf.is_file()
    from pypdf import PdfReader

    text = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
    assert phrase in text or "Awards" in text


@pytest.mark.skipif(not HAS_PDFLATEX, reason="pdflatex not on PATH")
def test_chat_compile_fail_rolls_back(app_client):
    app, client, projects_root = app_client
    pid = client.post("/api/projects", json={"kind": "blank"}).json()["id"]
    original = (projects_root / pid / "main.tex").read_text(encoding="utf-8")
    app.state.chat_model_override = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_project_file",
                        "args": {
                            "path": "main.tex",
                            "old_str": "Hello, Prism.",
                            "new_str": r"\thiscommanddoesnotexistXYZ",
                        },
                        "id": "call_undef",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Injected undefined command"),
        ]
    )
    events = _chat(client, pid, "Break the compile with a fake command")
    done = _done(events)
    assert done.get("verified") is False, done
    assert done.get("rollback") is True
    assert (projects_root / pid / "main.tex").read_text(encoding="utf-8") == original


def test_check_latex_reports_unbalanced_environment():
    from app.agent.verify import check_latex

    errs = check_latex(r"\begin{itemize}\item x")
    assert errs, "expected static errors for unmatched environment"


def test_snapshot_restore_roundtrip(projects_root: Path):
    from app.agent.verify import restore_text_files, snapshot_text_files
    from app.domain.project import ProjectStore

    store = ProjectStore(projects_root)
    proj = store.create(kind="blank", name="Snap")
    snap = snapshot_text_files(store, proj.id)
    store.write_file(proj.id, "main.tex", "BROKEN")
    store.write_file(proj.id, "extra.tex", "new")
    restore_text_files(store, proj.id, snap)
    assert store.read_file(proj.id, "main.tex").decode("utf-8") == snap["main.tex"].decode(
        "utf-8"
    )
    files = store.list_files(proj.id)
    assert "extra.tex" not in files
