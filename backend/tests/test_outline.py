from __future__ import annotations

from fastapi.testclient import TestClient

from prismtex.outline import outline_project, parse_tex_outline
from prismtex.workspace import ProjectStore


def test_parse_headings_levels_and_lines() -> None:
    text = "\n".join(
        [
            "\\chapter{Intro}",
            "\\section{Background}",
            "% \\section{Commented out}",
            "text \\subsection*{Starred} more",
            "\\subsubsection[short]{Long title}",
            "\\paragraph{Para}",
            "50\\% done \\section{After escaped percent}",
            "\\section{A} \\section{B}",
            "\\section{Nested {braces}}",
        ]
    )
    assert parse_tex_outline(text) == [
        {"title": "Intro", "line": 1, "level": 0, "macro": "chapter"},
        {"title": "Background", "line": 2, "level": 1, "macro": "section"},
        {"title": "Starred", "line": 4, "level": 2, "macro": "subsection*"},
        {"title": "Long title", "line": 5, "level": 3, "macro": "subsubsection"},
        {"title": "Para", "line": 6, "level": 4, "macro": "paragraph"},
        {"title": "After escaped percent", "line": 7, "level": 1, "macro": "section"},
        {"title": "A", "line": 8, "level": 1, "macro": "section"},
        {"title": "B", "line": 8, "level": 1, "macro": "section"},
    ]


def test_parse_handles_crlf() -> None:
    assert parse_tex_outline("a\r\n\\section{X}\r\n")[0]["line"] == 2


def test_outline_project_root_file_first(store: ProjectStore) -> None:
    pid = store.create(kind="paper").id
    store.write(pid, "appendix.tex", "\\section{Appendix}\n", None)
    store.write_bytes(pid, "binary.tex", b"\xff\xfe\\section{Hidden}")
    items = outline_project(store, pid)
    assert [(i["path"], i["title"]) for i in items] == [
        ("appendix.tex", "Appendix"),
        ("sections/conclusion.tex", "Conclusion"),
        ("sections/introduction.tex", "Introduction"),
        ("sections/method.tex", "Method"),
        ("sections/results.tex", "Results"),
    ]
    store.write(pid, "main.tex", "\\section{Root heading}\n", None)
    store.write(pid, "other.tex", "\\section{Other root}\n", None)
    store.update(pid, root_file="other.tex")
    assert outline_project(store, pid)[0] == {
        "title": "Other root",
        "line": 1,
        "level": 1,
        "macro": "section",
        "path": "other.tex",
    }


def test_outline_api(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"kind": "resume"}).json()["id"]
    response = client.get(f"/api/projects/{pid}/outline")
    assert response.status_code == 200
    outline = response.json()["outline"]
    assert [(i["path"], i["title"], i["macro"]) for i in outline] == [
        ("sections/education.tex", "Education", "section*"),
        ("sections/experience.tex", "Experience", "section*"),
        ("sections/skills.tex", "Skills", "section*"),
        ("sections/summary.tex", "Professional Summary", "section*"),
    ]
    assert client.get("/api/projects/" + "0" * 32 + "/outline").status_code == 404
