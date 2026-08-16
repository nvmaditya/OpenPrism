"""Phase 9: LaTeX outline titles + 1-based source lines."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

FIXTURE = """\\documentclass{article}
\\begin{document}
\\section{Intro}
Hello
\\subsection{Details}
% \\section{Commented Out}
\\section*{Appendix}
\\end{document}
"""


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def client(projects_root: Path) -> TestClient:
    return TestClient(create_app(Settings(prism_projects_root=projects_root)))


def test_parse_tex_outline_titles_and_one_based_lines():
    from app.outline.parse_tex import parse_tex_outline

    items = parse_tex_outline(FIXTURE)
    titles = [(i["title"], i["line"], i["level"]) for i in items]
    assert ("Intro", 3, 1) in titles
    assert ("Details", 5, 2) in titles
    assert ("Appendix", 7, 1) in titles
    assert not any(i["title"] == "Commented Out" for i in items)


def test_project_outline_api_reads_real_files(client: TestClient, projects_root: Path):
    pid = client.post("/api/projects", json={"kind": "blank"}).json()["id"]
    tex = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{First Heading}\n"
        "body\n"
        "\\subsection{Second Heading}\n"
        "\\end{document}\n"
    )
    put = client.put(
        f"/api/projects/{pid}/files/main.tex",
        json={"content": tex},
    )
    assert put.status_code == 200, put.text
    on_disk = (projects_root / pid / "main.tex").read_text(encoding="utf-8")
    assert "First Heading" in on_disk

    resp = client.get(f"/api/projects/{pid}/outline")
    assert resp.status_code == 200, resp.text
    items = resp.json()["outline"]
    by_title = {i["title"]: i for i in items}
    assert by_title["First Heading"]["line"] == 3
    assert by_title["First Heading"]["path"] == "main.tex"
    assert by_title["Second Heading"]["line"] == 5
    assert by_title["Second Heading"]["level"] == 2


def test_example_outline_includes_resume_headings(client: TestClient):
    pid = client.post("/api/projects", json={"kind": "example"}).json()["id"]
    resp = client.get(f"/api/projects/{pid}/outline")
    assert resp.status_code == 200, resp.text
    items = resp.json()["outline"]
    titles = {i["title"] for i in items}
    assert "Professional Summary" in titles
    assert "Experience" in titles
    assert "Education" in titles
    assert "Skills" in titles
    summary = next(i for i in items if i["title"] == "Professional Summary")
    assert summary["path"] == "sections/summary.tex"
    assert summary["line"] == 1
    assert summary["line"] >= 1


def test_outline_missing_project_404(client: TestClient):
    r = client.get(f"/api/projects/{'0' * 32}/outline")
    assert r.status_code == 404
