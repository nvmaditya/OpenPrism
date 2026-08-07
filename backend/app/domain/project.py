"""Project create/list/delete/import with isolated on-disk workspaces."""

from __future__ import annotations

import json
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Literal

from app.sandbox.paths import PathEscapeError, is_within, resolve_under

ProjectKind = Literal["blank", "example", "import"]

META_DIR = ".prism"
META_FILE = "meta.json"

BLANK_MAIN_TEX = r"""\documentclass[11pt,letterpaper]{article}
\begin{document}
Hello, Prism.
\end{document}
"""

EXAMPLE_MAIN_TEX = r"""\documentclass[10pt,letterpaper]{article}
\usepackage[margin=0.75in]{geometry}
\usepackage{titlesec}

\titleformat{\section}
  {\normalfont\large\bfseries}{}{0em}{}[\titlerule]

\begin{document}
\begin{center}
  {\LARGE Example Project}\\[0.5em]
  OpenPrism sample paper
\end{center}

\section*{Introduction}
This is the example project seeded by OpenPrism.

\section*{Methods}
Edit \texttt{main.tex} and compile when the TeX tools land.
\end{document}
"""


class ProjectError(Exception):
    """Domain error with an HTTP-friendly code."""

    def __init__(self, message: str, code: str = "project_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ProjectNotFound(ProjectError):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"project not found: {project_id}", code="not_found")


class ZipSlipError(ProjectError):
    def __init__(self, member: str) -> None:
        super().__init__(
            f"zip entry escapes project root: {member}",
            code="zip_slip",
        )


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    name: str
    kind: ProjectKind
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "created_at": self.created_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id() -> str:
    return uuid.uuid4().hex


class ProjectStore:
    """Filesystem-backed multi-project store under a single root."""

    def __init__(self, projects_root: Path) -> None:
        self.projects_root = Path(projects_root).resolve()
        self.projects_root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise ProjectNotFound(project_id)
        path = (self.projects_root / project_id).resolve()
        if not is_within(self.projects_root, path):
            raise ProjectNotFound(project_id)
        return path

    def _meta_path(self, project_dir: Path) -> Path:
        return project_dir / META_DIR / META_FILE

    def _read_meta(self, project_dir: Path) -> ProjectInfo:
        meta_path = self._meta_path(project_dir)
        if not meta_path.is_file():
            raise ProjectNotFound(project_dir.name)
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return ProjectInfo(
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            created_at=data["created_at"],
        )

    def _write_meta(self, project_dir: Path, info: ProjectInfo) -> None:
        meta_dir = project_dir / META_DIR
        meta_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path(project_dir).write_text(
            json.dumps(info.to_dict(), indent=2),
            encoding="utf-8",
        )

    def list_projects(self) -> list[ProjectInfo]:
        items: list[ProjectInfo] = []
        if not self.projects_root.is_dir():
            return items
        for child in sorted(self.projects_root.iterdir()):
            if not child.is_dir():
                continue
            meta = self._meta_path(child)
            if not meta.is_file():
                continue
            try:
                items.append(self._read_meta(child))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return items

    def get(self, project_id: str) -> ProjectInfo:
        project_dir = self._project_dir(project_id)
        if not project_dir.is_dir():
            raise ProjectNotFound(project_id)
        return self._read_meta(project_dir)

    def create(
        self,
        *,
        kind: ProjectKind = "blank",
        name: str | None = None,
    ) -> ProjectInfo:
        if kind not in ("blank", "example", "import"):
            raise ProjectError(f"invalid kind: {kind}", code="invalid_kind")
        project_id = _safe_id()
        project_dir = self.projects_root / project_id
        project_dir.mkdir(parents=True, exist_ok=False)

        display = name or ("Example Project" if kind == "example" else "New Project")
        info = ProjectInfo(
            id=project_id,
            name=display,
            kind=kind if kind != "import" else "import",
            created_at=_now_iso(),
        )
        self._write_meta(project_dir, info)

        uploads = project_dir / "prism-uploads"
        uploads.mkdir(exist_ok=True)

        if kind == "blank":
            (project_dir / "main.tex").write_text(BLANK_MAIN_TEX, encoding="utf-8")
        elif kind == "example":
            (project_dir / "main.tex").write_text(EXAMPLE_MAIN_TEX, encoding="utf-8")

        return info

    def delete(self, project_id: str) -> None:
        project_dir = self._project_dir(project_id)
        if not project_dir.is_dir():
            raise ProjectNotFound(project_id)
        # Ensure we only delete under projects_root
        if not is_within(self.projects_root, project_dir):
            raise ProjectNotFound(project_id)
        shutil.rmtree(project_dir)

    def resolve_file(self, project_id: str, relative: str) -> Path:
        project_dir = self._project_dir(project_id)
        if not project_dir.is_dir():
            raise ProjectNotFound(project_id)
        try:
            return resolve_under(project_dir, relative)
        except PathEscapeError as exc:
            raise ProjectError(str(exc), code="path_escape") from exc

    def read_file(self, project_id: str, relative: str) -> bytes:
        path = self.resolve_file(project_id, relative)
        if not path.is_file():
            raise ProjectError(f"file not found: {relative}", code="file_not_found")
        return path.read_bytes()

    def write_file(self, project_id: str, relative: str, content: bytes | str) -> Path:
        path = self.resolve_file(project_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return path

    def list_files(self, project_id: str) -> list[str]:
        project_dir = self._project_dir(project_id)
        if not project_dir.is_dir():
            raise ProjectNotFound(project_id)
        out: list[str] = []
        for path in sorted(project_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(project_dir).as_posix()
            if rel.startswith(f"{META_DIR}/"):
                continue
            out.append(rel)
        return out

    def import_zip(
        self,
        zip_stream: BinaryIO,
        *,
        name: str | None = None,
    ) -> ProjectInfo:
        """Create a project from a zip archive; reject zip-slip paths."""
        try:
            with zipfile.ZipFile(zip_stream) as zf:
                members = zf.infolist()
                for info in members:
                    self._assert_zip_member_safe(info.filename)

                project = self.create(kind="import", name=name or "Imported Project")
                project_dir = self._project_dir(project.id)
                try:
                    for info in members:
                        if info.is_dir() or info.filename.endswith("/"):
                            target = resolve_under(
                                project_dir, info.filename.rstrip("/")
                            )
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target = resolve_under(project_dir, info.filename)
                        if not is_within(project_dir, target):
                            raise ZipSlipError(info.filename)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info, "r") as src:
                            target.write_bytes(src.read())
                except Exception:
                    if project_dir.is_dir():
                        shutil.rmtree(project_dir, ignore_errors=True)
                    raise
                return project
        except zipfile.BadZipFile as exc:
            raise ProjectError("invalid zip archive", code="bad_zip") from exc
        except PathEscapeError as exc:
            raise ZipSlipError(str(exc)) from exc

    def _assert_zip_member_safe(self, filename: str) -> None:
        name = filename.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
            raise ZipSlipError(filename)
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." or p.startswith("..") for p in parts):
            raise ZipSlipError(filename)
