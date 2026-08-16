"""Walk .tex source for section headings (title + 1-based line)."""

from __future__ import annotations

import re
from typing import Any

from app.domain.project import ProjectError, ProjectStore

_LEVELS = {
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
}

_HEADING = re.compile(
    r"\\(?P<macro>chapter|section|subsection|subsubsection|paragraph)"
    r"(?P<star>\*)?"
    r"(?:\[[^\]]*\])?"
    r"\{(?P<title>[^{}]+)\}"
)


def _code_before_comment(line: str) -> str:
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "%" and (i == 0 or line[i - 1] != "\\"):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def parse_tex_outline(text: str) -> list[dict[str, Any]]:
    """Return heading dicts: title, line (1-based), level, macro."""
    items: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        code = _code_before_comment(raw)
        for m in _HEADING.finditer(code):
            macro = m.group("macro")
            star = m.group("star") or ""
            items.append(
                {
                    "title": m.group("title").strip(),
                    "line": lineno,
                    "level": _LEVELS[macro],
                    "macro": f"{macro}{star}",
                }
            )
    return items


def outline_project(store: ProjectStore, project_id: str) -> list[dict[str, Any]]:
    """Outline every .tex file in the project (main.tex first)."""
    store.get(project_id)
    tex_files = [p for p in store.list_files(project_id) if p.endswith(".tex")]
    tex_files.sort(key=lambda p: (p != "main.tex", p.replace("\\", "/")))
    out: list[dict[str, Any]] = []
    for rel in tex_files:
        try:
            text = store.read_file(project_id, rel).decode("utf-8")
        except (UnicodeDecodeError, ProjectError):
            continue
        for item in parse_tex_outline(text):
            out.append({**item, "path": rel.replace("\\", "/")})
    return out
