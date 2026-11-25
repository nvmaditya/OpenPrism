"""One-time startup migration of pre-PrismTeX project folders.

Old projects kept their metadata and logs in `projects/<id>/.prism/`, where
the file API and the agent could write it. Their metadata moves to
`state/<id>/project.json`; the old logs are dropped (ADR 0004).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prismtex.layout import DataLayout
from prismtex.workspace.store import ProjectInfo, ProjectKind, save_project_json
from prismtex.workspace.zipimport import detect_root_file

log = logging.getLogger("prismtex.migrate")

_PROJECT_ID = re.compile(r"[0-9a-f]{32}")
_KINDS: dict[str, ProjectKind] = {"example": "resume", "blank": "blank", "import": "import"}


def migrate_legacy_projects(layout: DataLayout) -> list[str]:
    """Migrate every `projects/<id>/.prism/meta.json`; returns the migrated ids.

    Idempotent: a migrated project has no `.prism/` left, so a second run
    finds nothing to do.
    """
    migrated: list[str] = []
    if not layout.projects.is_dir():
        return migrated
    for project_dir in sorted(layout.projects.iterdir()):
        legacy = project_dir / ".prism"
        if (
            not _PROJECT_ID.fullmatch(project_dir.name)
            or not project_dir.is_dir()
            or project_dir.is_symlink()
            or not (legacy / "meta.json").is_file()
        ):
            continue
        project_id = project_dir.name
        if not layout.project_json(project_id).exists():
            meta = _read_meta(legacy / "meta.json")
            save_project_json(layout.project_json(project_id), _info(project_id, project_dir, meta))
        if legacy.is_symlink():
            legacy.unlink()
        else:
            shutil.rmtree(legacy)
        log.info("Migrated legacy project %s (moved .prism/ metadata to state/)", project_id)
        migrated.append(project_id)
    return migrated


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _info(project_id: str, project_dir: Path, meta: dict[str, Any]) -> ProjectInfo:
    name = str(meta.get("name") or "").strip()[:120] or "Untitled project"
    created_at = str(meta.get("created_at") or _mtime_iso(project_dir))
    if (project_dir / "main.tex").is_file():
        root_file: str | None = "main.tex"
    else:
        root_file = detect_root_file(_source_paths(project_dir), lambda p: _read(project_dir, p))
    return ProjectInfo(
        id=project_id,
        name=name,
        kind=_KINDS.get(str(meta.get("kind")), "import"),
        created_at=created_at,
        updated_at=created_at,
        root_file=root_file,
        engine="pdflatex",
    )


def _source_paths(project_dir: Path) -> list[str]:
    return sorted(
        path.relative_to(project_dir).as_posix()
        for path in project_dir.rglob("*.tex")
        if path.is_file()
        and not path.is_symlink()
        and path.relative_to(project_dir).parts[0] not in (".prism", ".git")
    )


def _read(project_dir: Path, rel: str) -> str:
    try:
        return (project_dir / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="microseconds")
