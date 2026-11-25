"""Section headings of a project's `.tex` sources, with 1-based lines."""

from __future__ import annotations

import re
from typing import Any

from prismtex.errors import AppError
from prismtex.workspace import ProjectStore
from prismtex.workspace.text import strip_comment

__all__ = ["outline_project", "parse_tex_outline"]

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


def parse_tex_outline(text: str) -> list[dict[str, Any]]:
    """Heading dicts with title, line (1-based), level and macro (`section*` when starred)."""
    items: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        for match in _HEADING.finditer(strip_comment(raw)):
            macro = match.group("macro")
            items.append(
                {
                    "title": match.group("title").strip(),
                    "line": lineno,
                    "level": _LEVELS[macro],
                    "macro": f"{macro}{match.group('star') or ''}",
                }
            )
    return items


def outline_project(store: ProjectStore, project_id: str) -> list[dict[str, Any]]:
    """Headings of every `.tex` file: the root file first, then the rest by path."""
    root_file = store.settings(project_id).root_file
    tex_files = [e.path for e in store.list_files(project_id) if e.path.lower().endswith(".tex")]
    tex_files.sort(key=lambda path: (path != root_file, path))
    items: list[dict[str, Any]] = []
    for path in tex_files:
        try:
            text = store.read(project_id, path).text
        except AppError:
            continue  # binary, vanished or otherwise unreadable: not part of the outline
        items.extend({**item, "path": path} for item in parse_tex_outline(text))
    return items
