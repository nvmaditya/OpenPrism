"""Phase 4: TeX allowlist, compile, log retrieval."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sandbox.process import TEX_ALLOWLIST, ToolNotAllowlisted, resolve_allowlisted_executable
from app.settings import Settings

HAS_PDFLATEX = shutil.which("pdflatex") is not None
HAS_LATEXMK = shutil.which("latexmk") is not None
HAS_KPSEWHICH = shutil.which("kpsewhich") is not None


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def client(projects_root: Path) -> TestClient:
    return TestClient(create_app(Settings(prism_projects_root=projects_root)))


def _create(client: TestClient) -> str:
    r = client.post("/api/projects", json={"kind": "blank", "name": "Tex"})
    assert r.status_code == 200
    return r.json()["id"]


def test_tools_list_matches_allowlist(client: TestClient):
    r = client.get("/api/projects/tools")
    assert r.status_code == 200
    tools = set(r.json()["tools"])
    assert tools == set(TEX_ALLOWLIST)
    for name in (
        "latexmk",
        "pdflatex",
        "xelatex",
        "lualatex",
        "latex",
        "bibtex",
        "biber",
        "makeindex",
        "dvipdfmx",
        "kpsewhich",
    ):
        assert name in tools


def test_non_allowlisted_tool_rejected(client: TestClient):
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/tools/run",
        json={"tool": "curl", "args": ["https://example.com"]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "not_allowlisted"

    r2 = client.post(
        f"/api/projects/{pid}/tools/run",
        json={"tool": "bash", "args": ["-c", "echo hi"]},
    )
    assert r2.status_code == 400
    assert r2.json()["detail"]["code"] == "not_allowlisted"

    with pytest.raises(ToolNotAllowlisted):
        resolve_allowlisted_executable("rm")


def test_path_like_tool_name_rejected(client: TestClient):
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/tools/run",
        json={"tool": "C:\\Windows\\System32\\cmd.exe", "args": []},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "not_allowlisted"


@pytest.mark.skipif(not HAS_KPSEWHICH, reason="kpsewhich not on PATH")
def test_kpsewhich_runs_and_log_stored(client: TestClient, projects_root: Path):
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/tools/run",
        json={"tool": "kpsewhich", "args": ["article.cls"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == "kpsewhich"
    assert "log_id" in body
    assert "log" in body
    assert body["log"]
    log_id = body["log_id"]

    fetched = client.get(f"/api/projects/{pid}/logs/{log_id}")
    assert fetched.status_code == 200
    assert fetched.json()["log_id"] == log_id
    assert "kpsewhich" in fetched.json()["log"]
    assert (projects_root / pid / ".prism" / "logs" / f"{log_id}.log").is_file()


@pytest.mark.skipif(not (HAS_LATEXMK or HAS_PDFLATEX), reason="no TeX compiler on PATH")
def test_compile_minimal_tex_and_fetch_log(client: TestClient, projects_root: Path):
    pid = _create(client)
    # blank project already has main.tex
    engine = "latexmk" if HAS_LATEXMK else "pdflatex"
    r = client.post(
        f"/api/projects/{pid}/compile",
        json={"engine": engine, "main_file": "main.tex"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["log_id"]
    assert body["engine"] == engine
    assert isinstance(body["log"], str) and len(body["log"]) > 0
    # Log should mention engine or source
    log_l = body["log"].lower()
    assert "main.tex" in log_l or engine in log_l or "pdflatex" in log_l or "latex" in log_l

    # Missing binary is not claimed success: if success, PDF should exist for pdf engines
    if body["success"]:
        assert body.get("pdf_path") == "main.pdf" or engine == "latex"
        assert (projects_root / pid / "main.pdf").is_file() or engine == "latex"
    else:
        # still must not pretend missing binary was ok without log
        assert body["returncode"] != 0 or "error" in log_l or "fatal" in log_l

    # Re-fetch log
    got = client.get(f"/api/projects/{pid}/logs/{body['log_id']}")
    assert got.status_code == 200
    assert got.json()["log"] == body["log"] or len(got.json()["log"]) > 0

    listed = client.get(f"/api/projects/{pid}/logs")
    assert listed.status_code == 200
    ids = {x["log_id"] for x in listed.json()["logs"]}
    assert body["log_id"] in ids


@pytest.mark.skipif(not HAS_PDFLATEX, reason="pdflatex not on PATH")
def test_compile_pdflatex_engine(client: TestClient):
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/compile",
        json={"engine": "pdflatex", "main_file": "main.tex"},
    )
    assert r.status_code == 200
    assert r.json()["engine"] == "pdflatex"
    assert r.json()["log"]
    # Prefer success on this host (MiKTeX present)
    assert r.json()["success"] is True
    assert r.json()["pdf_path"] == "main.pdf"


def test_compile_missing_main_file(client: TestClient):
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/compile",
        json={"engine": "pdflatex", "main_file": "nope.tex"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "file_not_found"


def test_health(client: TestClient):
    assert client.get("/health").status_code == 200
