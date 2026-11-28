"""ProjectStore: projects, their settings and their files.

Every file operation goes through the path policy, refuses `.git/` and
`.prism/` at the project root, and writes atomically. Project settings
live in `state/<id>/project.json`, outside the project folder (ADR 0004).
"""

from __future__ import annotations

import enum
import errno
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, get_args

from prismtex.errors import (
    AppError,
    FileBusy,
    FileExists,
    FileNotFound,
    InvalidRequest,
    NotText,
    PathPolicyError,
    PayloadTooLarge,
    ProjectNotFound,
    VersionConflict,
)
from prismtex.layout import DataLayout
from prismtex.workspace.paths import (
    INTERNAL_DIRS,
    SOURCE_WRITE_EXTENSIONS,
    check_relative_path,
    check_root_file,
    is_internal,
    resolve_in,
)
from prismtex.workspace.text import (
    FileVersion,
    content_version,
    decode_text,
    detect_eol,
    encode_text,
)
from prismtex.workspace.zipimport import ValidatedZip, detect_root_file

Engine = Literal["pdflatex", "xelatex", "lualatex"]
ProjectKind = Literal["blank", "paper", "resume", "import"]
TemplateKind = Literal["blank", "paper", "resume"]
ENGINES: frozenset[str] = frozenset(get_args(Engine))
TEMPLATE_KINDS: frozenset[str] = frozenset(get_args(TemplateKind))
AgentProfile = Literal["agentic", "guided"]
AGENT_PROFILES: frozenset[str] = frozenset(get_args(AgentProfile))
SOURCE_EXTENSIONS = SOURCE_WRITE_EXTENSIONS
MAX_TEXT_BYTES = 10 * 2**20
MAX_NAME = 120
SCHEMA = 1

# Atomic writes land in a temp file in the target's own folder, then os.replace.
# A crash can leave one behind, so the name is reserved: it never shows in a
# listing and cannot be read or written through the file API.
TEMP_PREFIX = ".prismtex-"
TEMP_SUFFIX = ".tmp"

# A reader (usually a compile) can hold a file open on Windows without granting
# delete sharing, so os.replace/os.rename/unlink raise PermissionError. A short
# retry rides out a compile pass; if it persists the caller gets a 409, not 500.
_WRITE_RETRIES = 10
_WRITE_RETRY_DELAY_S = 0.05

_PROJECT_ID = re.compile(r"[0-9a-f]{32}")
_DEFAULT_NAMES: dict[str, str] = {
    "blank": "Untitled project",
    "paper": "Example paper",
    "resume": "Example résumé",
    "import": "Imported project",
}


class Unset(enum.Enum):
    UNSET = "UNSET"


UNSET: Final = Unset.UNSET


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    name: str
    kind: ProjectKind
    created_at: str
    updated_at: str
    root_file: str | None
    engine: Engine

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "root_file": self.root_file,
            "engine": self.engine,
        }


@dataclass(frozen=True)
class AgentSettings:
    """Per-project agent choices (agent design §10.3); None means the app default."""

    model: str | None = None
    profile: AgentProfile | None = None
    auto_accept: bool = False

    def __post_init__(self) -> None:
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("model must be a non-empty string or None")
        if self.profile is not None and self.profile not in AGENT_PROFILES:
            raise ValueError("profile must be 'agentic', 'guided' or None")
        if not isinstance(self.auto_accept, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("auto_accept must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "profile": self.profile, "auto_accept": self.auto_accept}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AgentSettings:
        """Build from stored or request JSON; raises ValueError on a bad value."""
        model = data.get("model")
        profile = data.get("profile")
        auto_accept = data.get("auto_accept", False)
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or None")
        if profile is not None and profile not in AGENT_PROFILES:
            raise ValueError("profile must be 'agentic', 'guided' or None")
        if not isinstance(auto_accept, bool):
            raise ValueError("auto_accept must be a boolean")
        checked: AgentProfile | None = profile  # pyright: ignore[reportAssignmentType]
        return cls(model=model, profile=checked, auto_accept=auto_accept)


@dataclass(frozen=True)
class ProjectSettings:
    root_file: str | None
    engine: Engine
    agent: AgentSettings = AgentSettings()


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    kind: Literal["source", "asset"]

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "kind": self.kind}


def file_kind(path: str) -> Literal["source", "asset"]:
    return "source" if Path(path).suffix.lower() in SOURCE_EXTENSIONS else "asset"


class ProjectStore:
    def __init__(
        self,
        layout: DataLayout,
        *,
        max_upload_bytes: int = 50 * 2**20,
        import_max_bytes: int = 200 * 2**20,
        import_max_files: int = 5000,
    ) -> None:
        self.layout = layout
        self.max_upload_bytes = max_upload_bytes
        self.import_max_bytes = import_max_bytes
        self.import_max_files = import_max_files
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # -- projects -----------------------------------------------------------

    def create(self, *, kind: TemplateKind = "blank", name: str | None = None) -> ProjectInfo:
        if kind not in TEMPLATE_KINDS:
            raise InvalidRequest("Choose blank, paper or resume.", field="kind")
        info = self._new_info(kind, name, root_file="main.tex")
        project_dir = self.layout.project_dir(info.id)
        try:
            template = resources.files("prismtex.workspace").joinpath("templates", kind)
            with resources.as_file(template) as source:
                shutil.copytree(source, project_dir, ignore=shutil.ignore_patterns("__pycache__"))
            self._save(info)
        except BaseException:
            self._remove(info.id)
            raise
        return info

    def import_zip(self, stream: BinaryIO, *, name: str | None = None) -> ProjectInfo:
        with ValidatedZip.open(
            stream, max_bytes=self.import_max_bytes, max_files=self.import_max_files
        ) as archive:
            info = self._new_info("import", name, root_file=None)
            project_dir = self.layout.project_dir(info.id)
            try:
                project_dir.mkdir(parents=True)
                archive.extract_to(project_dir)
                root_file = detect_root_file(archive.paths, lambda p: _read_text(project_dir, p))
                info = replace(info, root_file=root_file)
                self._save(info)
            except BaseException:
                self._remove(info.id)
                raise
        return info

    def list(self) -> list[ProjectInfo]:
        """Every readable project, newest first; a broken project.json is skipped."""
        infos: list[ProjectInfo] = []
        if not self.layout.state_root.is_dir():
            return infos
        for child in self.layout.state_root.iterdir():
            if not _PROJECT_ID.fullmatch(child.name):
                continue
            try:
                infos.append(self.get(child.name))
            except ProjectNotFound:
                continue
        infos.sort(key=lambda info: (_parse_time(info.created_at), info.id), reverse=True)
        return infos

    def get(self, project_id: str) -> ProjectInfo:
        self._check_id(project_id)
        info = load_project_json(self.layout.project_json(project_id), project_id)
        if info is None or not self.layout.project_dir(project_id).is_dir():
            raise ProjectNotFound(project_id)
        return info

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        root_file: str | None | Unset = UNSET,
        engine: Engine | None = None,
    ) -> ProjectInfo:
        with self._lock(project_id):
            info = self.get(project_id)
            changes: dict[str, Any] = {}
            if name is not None:
                changes["name"] = _clean_name(name)
            if root_file is not UNSET:
                changes["root_file"] = (
                    None
                    if root_file is None
                    else check_root_file(self.layout.project_dir(project_id), root_file)
                )
            if engine is not None:
                if engine not in ENGINES:
                    raise InvalidRequest(
                        "The engine must be pdflatex, xelatex or lualatex.", field="engine"
                    )
                changes["engine"] = engine
            info = replace(info, updated_at=_now(), **changes)
            self._save(info)
            return info

    def settings(self, project_id: str) -> ProjectSettings:
        info = self.get(project_id)
        agent = load_agent_settings(self.layout.project_json(project_id))
        return ProjectSettings(root_file=info.root_file, engine=info.engine, agent=agent)

    def update_settings(
        self,
        project_id: str,
        *,
        root_file: str | None | Unset = UNSET,
        engine: Engine | None = None,
        agent: AgentSettings | None = None,
    ) -> ProjectSettings:
        """Change settings in one read-modify-write of project.json.

        Takes the same per-project lock as `update`, and every save keeps the
        fields it does not change, so neither drops the other's values.
        """
        with self._lock(project_id):
            if root_file is not UNSET or engine is not None:
                self.update(project_id, root_file=root_file, engine=engine)
            if agent is not None:
                info = replace(self.get(project_id), updated_at=_now())
                save_project_json(self.layout.project_json(project_id), info, agent=agent)
            return self.settings(project_id)

    def delete(self, project_id: str) -> None:
        with self._lock(project_id):
            self.get(project_id)
            self._remove(project_id)

    def project_dir(self, project_id: str) -> Path:
        """The project folder, for compiling and History only."""
        self.get(project_id)
        return self.layout.project_dir(project_id)

    # -- files --------------------------------------------------------------

    def list_files(self, project_id: str) -> list[FileEntry]:
        root = self.project_dir(project_id)
        entries = [
            FileEntry(rel, size, file_kind(rel)) for rel, size in _walk_files(root.resolve())
        ]
        return sorted(entries, key=lambda entry: entry.path)

    def tree(self, project_id: str, subpath: str = "") -> dict[str, Any]:
        info = self.get(project_id)
        root = self.layout.project_dir(project_id).resolve()
        if not subpath:
            return _tree_node(root, root, name=info.name, rel="")
        rel, target = self._locate(project_id, subpath)
        if not target.exists():
            raise FileNotFound(rel)
        if not target.is_dir():
            raise InvalidRequest(f"'{rel}' is a file, not a folder.", field="path")
        return _tree_node(root, target, name=target.name, rel=rel)

    def read(self, project_id: str, path: str) -> FileVersion:
        rel, target = self._locate(project_id, path)
        data = _read_file(target, rel)
        try:
            text, eol = decode_text(data)
        except UnicodeDecodeError as exc:
            raise NotText(rel) from exc
        return FileVersion(text=text, version=content_version(data), eol=eol)

    def read_bytes(self, project_id: str, path: str) -> tuple[bytes, str]:
        rel, target = self._locate(project_id, path)
        data = _read_file(target, rel)
        return data, content_version(data)

    def write(self, project_id: str, path: str, text: str, base_version: str | None) -> FileVersion:
        """Write LF text, keeping the file's line endings.

        With a `base_version`, the write happens only if the file on disk still
        has that version; otherwise VersionConflict carries the current one.
        """
        rel, target = self._locate(project_id, path)
        normalized = _prepare_text(text)
        with self._lock(project_id):
            current = target.read_bytes() if target.is_file() else None
            if target.exists() and current is None:
                raise InvalidRequest(f"'{rel}' is a folder.", field="path")
            current_version = content_version(current) if current is not None else None
            if base_version is not None and base_version != current_version:
                raise VersionConflict(rel, current_version)
            eol = detect_eol(current) if current is not None else "lf"
            data = encode_text(normalized, eol)
            _ensure_parent(self.layout.project_dir(project_id), rel, target)
            _atomic_write(target, data, rel)
        return FileVersion(text=normalized, version=content_version(data), eol=eol)

    def write_new(self, project_id: str, path: str, text: str) -> FileVersion:
        """Create an LF text file; VersionConflict (with its version) if one exists.

        Accepting a Proposal that creates a file uses this, so a file the user
        made in the meantime is never overwritten.
        """
        rel, target = self._locate(project_id, path)
        normalized = _prepare_text(text)
        data = encode_text(normalized, "lf")
        with self._lock(project_id):
            if target.is_file():
                raise VersionConflict(rel, content_version(target.read_bytes()))
            if target.exists():
                raise InvalidRequest(f"'{rel}' is a folder.", field="path")
            _ensure_parent(self.layout.project_dir(project_id), rel, target)
            _atomic_write(target, data, rel)
        return FileVersion(text=normalized, version=content_version(data), eol="lf")

    def write_bytes(self, project_id: str, path: str, data: bytes) -> FileEntry:
        rel, target = self._locate(project_id, path)
        if len(data) > self.max_upload_bytes:
            raise PayloadTooLarge(self.max_upload_bytes / 2**20)
        with self._lock(project_id):
            if target.is_dir():
                raise InvalidRequest(f"'{rel}' is a folder.", field="path")
            _ensure_parent(self.layout.project_dir(project_id), rel, target)
            _atomic_write(target, data, rel)
        return FileEntry(rel, len(data), file_kind(rel))

    def upload(self, project_id: str, *, filename: str, data: bytes, folder: str = "") -> FileEntry:
        """Store an uploaded file at `<folder>/<basename of filename>`, overwriting."""
        basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        check_relative_path(basename)
        rel = f"{check_relative_path(folder)}/{basename}" if folder.strip("/\\") else basename
        return self.write_bytes(project_id, rel, data)

    def rename(self, project_id: str, src: str, dst: str) -> None:
        """Move a file or folder; refuses to replace anything at `dst`."""
        src_rel, src_path = self._locate_entry(project_id, src)
        dst_rel, dst_path = self._locate_entry(project_id, dst)
        with self._lock(project_id):
            if not os.path.lexists(src_path):
                raise FileNotFound(src_rel)
            if src_rel == dst_rel:
                return
            if (dst_rel + "/").startswith(src_rel + "/"):
                raise InvalidRequest("A folder cannot be moved into itself.", field="to")
            if os.path.lexists(dst_path) and not _same_entry(src_path, dst_path):
                raise FileExists(dst_rel)
            _ensure_parent(self.layout.project_dir(project_id), dst_rel, dst_path)
            _mutate_path(lambda: os.rename(src_path, dst_path), src_rel)

    def delete_file(self, project_id: str, path: str, *, base_version: str | None = None) -> None:
        """Delete a file, or a folder with everything in it.

        With a `base_version`, only a regular file whose bytes still have that
        version is deleted; otherwise VersionConflict carries the current
        version (None when the file is gone).
        """
        rel, target = self._locate_entry(project_id, path)
        with self._lock(project_id):
            if base_version is not None:
                if target.is_symlink() or target.is_dir():
                    raise InvalidRequest(f"'{rel}' is not a regular file.", field="path")
                current = content_version(target.read_bytes()) if target.is_file() else None
                if current != base_version:
                    raise VersionConflict(rel, current)
                _mutate_path(target.unlink, rel)
                return
            if target.is_symlink() or target.is_file():
                _mutate_path(target.unlink, rel)
            elif target.is_dir():
                _rmtree(target)
            else:
                raise FileNotFound(rel)

    def delete_files(self, project_id: str, paths: list[str]) -> dict[str, Any]:
        self.get(project_id)
        deleted: list[str] = []
        errors: list[dict[str, str]] = []
        for path in paths:
            try:
                self.delete_file(project_id, path)
            except AppError as exc:
                errors.append({"path": path, "code": exc.code, "message": exc.message})
            else:
                deleted.append(path)
        return {"deleted": deleted, "errors": errors}

    # -- internals ----------------------------------------------------------

    def _check_id(self, project_id: str) -> None:
        if not _PROJECT_ID.fullmatch(project_id):
            raise ProjectNotFound(project_id)

    def _lock(self, project_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.RLock())

    def _checked_rel(self, path: str) -> str:
        rel = check_relative_path(path)
        if is_internal(rel):
            raise PathPolicyError(path, "internal", "This folder is reserved and cannot be used.")
        if any(_is_temp_name(seg) for seg in rel.split("/")):
            raise PathPolicyError(path, "internal", "This name is reserved and cannot be used.")
        return rel

    def _locate(self, project_id: str, path: str) -> tuple[str, Path]:
        """(normalized path, fully resolved target inside the project)."""
        root = self.project_dir(project_id)
        rel = self._checked_rel(path)
        return rel, resolve_in(root, rel)

    def _locate_entry(self, project_id: str, path: str) -> tuple[str, Path]:
        """Like _locate, but the last segment is not followed if it is a symlink.

        Deleting or renaming a symlink acts on the link, never on its target.
        """
        root = self.project_dir(project_id)
        rel = self._checked_rel(path)
        parent, _, name = rel.rpartition("/")
        base = resolve_in(root, parent) if parent else root.resolve()
        return rel, base / name

    def _new_info(
        self, kind: ProjectKind, name: str | None, *, root_file: str | None
    ) -> ProjectInfo:
        now = _now()
        return ProjectInfo(
            id=uuid.uuid4().hex,
            name=_DEFAULT_NAMES[kind] if name is None or not name.strip() else _clean_name(name),
            kind=kind,
            created_at=now,
            updated_at=now,
            root_file=root_file,
            engine="pdflatex",
        )

    def _save(self, info: ProjectInfo) -> None:
        save_project_json(self.layout.project_json(info.id), info)

    def _remove(self, project_id: str) -> None:
        # State goes last: if removing the files is interrupted, the project is
        # still listed and the delete can be retried.
        for path in (
            self.layout.project_dir(project_id),
            self.layout.build(project_id),
            self.layout.history_git(project_id),
            self.layout.state(project_id),
        ):
            if path.exists():
                _rmtree(path)


# -- project.json -------------------------------------------------------------


def save_project_json(path: Path, info: ProjectInfo, *, agent: AgentSettings | None = None) -> None:
    """Write the project's info, keeping every other field already in the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _read_json_object(path)
    body.update({"schema": SCHEMA, **info.to_dict()})
    if agent is not None:
        body["agent"] = agent.to_dict()
    _atomic_write(path, (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def load_agent_settings(path: Path) -> AgentSettings:
    """The stored agent settings; the defaults when missing or unreadable."""
    raw = _read_json_object(path).get("agent")
    if not isinstance(raw, dict):
        return AgentSettings()
    try:
        return AgentSettings.from_dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    except ValueError:
        return AgentSettings()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _prepare_text(text: str) -> str:
    """LF text checked for valid Unicode and the size limit."""
    normalized = text.replace("\r\n", "\n")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise InvalidRequest("The text is not valid Unicode.", field="content") from exc
    if size > MAX_TEXT_BYTES:
        raise PayloadTooLarge(MAX_TEXT_BYTES / 2**20)
    return normalized


def load_project_json(path: Path, project_id: str) -> ProjectInfo | None:
    """The project's info, or None when the file is missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        info = ProjectInfo(
            id=str(data["id"]),
            name=str(data["name"]),
            kind=data["kind"],
            created_at=str(data["created_at"]),
            updated_at=str(data.get("updated_at", data["created_at"])),
            root_file=data.get("root_file"),
            engine=data.get("engine", "pdflatex"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    valid = (
        info.id == project_id
        and info.kind in get_args(ProjectKind)
        and info.engine in ENGINES
        and (info.root_file is None or isinstance(info.root_file, str))
    )
    return info if valid else None


# -- helpers ------------------------------------------------------------------

_clock_guard = threading.Lock()
_last_time = datetime.min.replace(tzinfo=UTC)


def _now() -> str:
    """UTC ISO 8601 with microseconds, strictly increasing within the process.

    Strictly increasing so that projects created back to back sort in
    creation order even on clocks with coarse resolution.
    """
    global _last_time
    with _clock_guard:
        now = datetime.now(UTC)
        if now <= _last_time:
            now = _last_time + timedelta(microseconds=1)
        _last_time = now
    return now.isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= MAX_NAME:
        raise InvalidRequest(f"The name must be 1 to {MAX_NAME} characters.", field="name")
    return cleaned


def _read_file(target: Path, rel: str) -> bytes:
    if not target.is_file():
        raise FileNotFound(rel)
    try:
        return target.read_bytes()
    except FileNotFoundError as exc:
        raise FileNotFound(rel) from exc


def _read_text(root: Path, rel: str) -> str:
    try:
        return resolve_in(root, rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError, PathPolicyError):
        return ""


def _ensure_parent(root: Path, rel: str, target: Path) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise InvalidRequest(f"A folder on the way to '{rel}' is a file.", field="path") from exc
    # mkdir may have walked through a symlinked folder created meanwhile.
    resolve_in(root, rel)


def _atomic_write(target: Path, data: bytes, rel: str | None = None) -> None:
    mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else 0o644
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        _mutate_path(lambda: os.replace(tmp, target), rel or target.name)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _is_temp_name(name: str) -> bool:
    """True for a name reserved by `_atomic_write`'s temp files."""
    return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)


def _mutate_path(action: Callable[[], None], rel: str) -> None:
    """Run a file mutation (replace, rename, unlink), handling two OS quirks.

    A Windows sharing violation (PermissionError) is retried briefly, then
    turned into a 409 FileBusy. A name too long for the file system
    (ENAMETOOLONG) becomes a 400 PathPolicyError instead of a 500.
    """
    for attempt in range(_WRITE_RETRIES):
        try:
            action()
            return
        except PermissionError:
            if attempt + 1 == _WRITE_RETRIES:
                raise FileBusy(rel) from None
            time.sleep(_WRITE_RETRY_DELAY_S)
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                raise PathPolicyError(
                    rel, "too_long", "The file name or path is too long for this file system."
                ) from exc
            raise


def remove_stale_temp_files(layout: DataLayout) -> None:
    """Delete atomic-write temp files left by a crash, run once at startup.

    Startup is single-threaded and precedes any serving, so no write is in
    flight; every `.prismtex-*.tmp` is a leftover and safe to remove.
    """
    if not layout.projects.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(layout.projects):
        for name in filenames:
            if _is_temp_name(name):
                try:
                    Path(dirpath, name).unlink()
                except OSError:
                    pass


def _rmtree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return

    def retry_writable(func: Any, failed: str, _exc: BaseException) -> None:
        # Windows refuses to delete read-only files (git objects are read-only).
        os.chmod(failed, stat.S_IWRITE)
        func(failed)

    shutil.rmtree(path, onexc=retry_writable)


def _same_entry(a: Path, b: Path) -> bool:
    # A case-only rename on a case-insensitive disk sees the destination as
    # existing; it is the same file, so the rename may go ahead.
    try:
        return a.name.lower() == b.name.lower() and os.path.samefile(a, b)
    except OSError:
        return False


def _hidden(name: str) -> bool:
    return name.lower() in INTERNAL_DIRS


def _walk_files(root: Path) -> Iterator[tuple[str, int]]:
    """(relative path, size) of every visible file; never follows a symlinked folder."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _hidden(d)]
        base = Path(dirpath)
        for filename in filenames:
            if _is_temp_name(filename):
                continue
            full = base / filename
            if not _contained(root, full):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            yield full.relative_to(root).as_posix(), size


def _contained(root: Path, path: Path) -> bool:
    if not path.is_symlink():
        return True
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _tree_node(root: Path, folder: Path, *, name: str, rel: str) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        entries = []
    dirs = sorted(
        (e for e in entries if e.is_dir(follow_symlinks=False) and not _hidden(e.name)),
        key=lambda e: (e.name.lower(), e.name),
    )
    files = sorted(
        (
            e
            for e in entries
            if e.is_file() and not _is_temp_name(e.name) and _contained(root, Path(e.path))
        ),
        key=lambda e: (e.name.lower(), e.name),
    )
    for entry in dirs:
        child_rel = f"{rel}/{entry.name}" if rel else entry.name
        children.append(_tree_node(root, Path(entry.path), name=entry.name, rel=child_rel))
    for entry in files:
        child_rel = f"{rel}/{entry.name}" if rel else entry.name
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        children.append(
            {
                "name": entry.name,
                "path": child_rel,
                "type": "file",
                "size": size,
                "kind": file_kind(entry.name),
            }
        )
    return {"name": name, "path": rel, "type": "dir", "children": children}
