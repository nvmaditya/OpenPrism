"""Staging: a Turn's base and the overlay its edits land in (agent design §2.2 `staging`, §6.2).

A Turn never writes the Project folder (invariant I1). `capture_base` takes
every Source file once when the Turn starts, with a dirty editor buffer in
place of its file; `Staging.open` lays the open Proposal's undecided Hunks
over that base as each file's start text; tools then read and write the
overlay. `Staging.files` hands the result to `proposals.compute`.

Every write goes through the path policy and the extension allowlist
(`check_source_path`), a 1 MiB limit and a control-character check, and
raises ToolError with a stable code when one of them refuses (invariant I6).
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from prismtex.agent.proposals import (
    Hunk,
    Proposal,
    join_lines,
    place,
    split_lines,
)
from prismtex.agent.text import control_char, view_id
from prismtex.agent.types import EditorContext
from prismtex.compile.tree import SourceTree, source_tree
from prismtex.errors import FileNotFound, NotText, PathDenied, RootFileMissing
from prismtex.workspace import (
    Engine,
    Eol,
    ProjectStore,
    check_source_path,
    content_version,
    encode_text,
)

__all__ = [
    "MAX_WRITE_BYTES",
    "BaseFile",
    "CarryReport",
    "StagedChange",
    "StagedFile",
    "StagedText",
    "Staging",
    "ToolError",
    "ToolErrorCode",
    "TurnBase",
    "capture_base",
]

MAX_WRITE_BYTES: Final = 2**20

ToolErrorCode = Literal[
    "path_denied", "write_denied", "not_found", "binary", "exists", "too_large", "control_char"
]
By = Literal["model", "host"]


class ToolError(Exception):
    """A staging read or write that was refused; nothing was changed.

    `code` is one of ToolErrorCode, `message` says why in plain words for the
    model, and `path` is the path as it was given.
    """

    def __init__(self, code: ToolErrorCode, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code: ToolErrorCode = code
        self.message = message
        self.path = path


@dataclass(frozen=True)
class BaseFile:
    """One Source file as the Turn found it.

    `text` is LF text and `version` the content version of its bytes. For
    `source="buffer"` it is the dirty editor buffer, with the disk file's
    line ending, and `version` is the version the file will have once that
    buffer is saved.
    """

    path: str
    text: str
    version: str
    eol: Eol
    source: Literal["disk", "buffer"]


@dataclass(frozen=True)
class TurnBase:
    """Every Source file of a Project, captured once when a Turn starts.

    `assets` lists the files the Turn sees only by name: Assets, and Source
    files that are not UTF-8 text; their bytes are never loaded. `root_file`
    None means nothing is compiled. `projects` is the store the tree is built
    over.
    """

    project_id: str
    root_file: str | None
    engine: Engine
    files: Mapping[str, BaseFile]
    assets: tuple[str, ...]
    projects: ProjectStore = field(repr=False, compare=False)

    def tree(self) -> SourceTree:
        """The Baseline tree: every Source file overlaid with its base text.

        RootFileMissing when `root_file` is None.
        """
        overlay: dict[str, str | None] = {p: f.text for p, f in self.files.items()}
        return _tree(self, overlay)


def capture_base(projects: ProjectStore, project_id: str, editor: EditorContext | None) -> TurnBase:
    """Read every Source file of the Project once, with a dirty buffer in place of its file.

    Paths the agent may not read are left out. A Source file that is not
    UTF-8 is listed with the Assets. The buffer counts only for a Source file
    on disk; equal to that file's bytes, it is the disk file.
    """
    settings = projects.settings(project_id)
    files: dict[str, BaseFile] = {}
    assets: list[str] = []
    for entry in projects.list_files(project_id):
        try:
            path = check_source_path(entry.path, for_write=False)
        except PathDenied:
            continue
        if entry.kind == "asset":
            assets.append(path)
            continue
        try:
            read = projects.read(project_id, path)
        except NotText:
            assets.append(path)
            continue
        except FileNotFound:
            continue  # removed since it was listed
        files[path] = BaseFile(path, read.text, read.version, read.eol, "disk")

    if editor is not None and editor.buffer is not None:
        try:
            path = check_source_path(editor.path, for_write=False)
        except PathDenied:
            path = None
        disk = files.get(path) if path is not None else None
        if path is not None and disk is not None:
            text = editor.buffer.replace("\r\n", "\n")
            version = content_version(encode_text(text, disk.eol))
            if version != disk.version:
                files[path] = BaseFile(path, text, version, disk.eol, "buffer")

    return TurnBase(
        project_id=project_id,
        root_file=settings.root_file,
        engine=settings.engine,
        files=MappingProxyType(files),
        assets=tuple(sorted(assets)),
        projects=projects,
    )


@dataclass(frozen=True)
class StagedText:
    """A file as the Turn sees it; `view_id` is 6 hex of sha256 over the text."""

    path: str
    text: str
    view_id: str


@dataclass(frozen=True)
class StagedChange:
    """What one write did.

    `span` holds the first and last changed line of the new text (1-based,
    inclusive); for a change that only removed lines it is (n, n - 1), where
    n is the line after the removed ones. It is None for a delete and for a
    write that changed nothing.
    """

    path: str
    op: Literal["modify", "create", "delete"]
    span: tuple[int, int] | None


@dataclass(frozen=True)
class StagedFile:
    """One changed path: its base, its start text and its staged text.

    `start` is the text the Turn started from (the base with the carried
    Hunks applied) and `staged` the text it ends with; None means the file
    does not exist. `host_lines` are the lines host writes put in the file.
    """

    path: str
    base: BaseFile | None
    start: str | None
    staged: str | None
    host_lines: tuple[str, ...]


@dataclass(frozen=True)
class CarryReport:
    """What became of the open Proposal's undecided Hunks when a Turn started.

    `carried` Hunks are in the start text (applied, or already in the base);
    `dropped` ones no longer apply and are left out.
    """

    carried: tuple[str, ...]
    dropped: tuple[str, ...]


class Staging:
    """The overlay of one Turn's edits over its TurnBase.

    Reads see the staged text of a file the Turn changed, and its start text
    otherwise. Nothing here reaches the disk.
    """

    def __init__(
        self,
        base: TurnBase,
        start: Mapping[str, str | None] | None = None,
        host_lines: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        """Prefer `Staging.open`. `start` holds the paths whose start text differs from the base."""
        self._base = base
        self._assets = frozenset(base.assets)
        self._start: dict[str, str | None] = dict(start or {})
        self._staged: dict[str, str | None] = {}
        self._host: dict[str, dict[str, None]] = {
            path: dict.fromkeys(lines) for path, lines in (host_lines or {}).items()
        }
        self._changes = 0

    @classmethod
    def open(cls, base: TurnBase, carry: Proposal | None) -> tuple[Staging, CarryReport]:
        """A Staging over `base`, starting from the undecided Hunks of `carry`.

        Each undecided Hunk is rebased onto the base, as `resolve` would: one
        that applies (exactly or shifted) is part of the start text, one whose
        lines are there already is carried as is, and one that no longer
        applies is dropped and reported.
        """
        start: dict[str, str | None] = {}
        host: dict[str, list[str]] = {}
        carried: list[str] = []
        dropped: list[str] = []
        undecided = carry.undecided if carry is not None else frozenset[str]()
        for change in carry.files if carry is not None else ():
            hunks = [h for h in change.hunks if h.id in undecided]
            if not hunks:
                continue
            carry_file = _carry(base, change.op, change.path, change.new_text, hunks)
            if carry_file.changed:
                start[change.path] = carry_file.text
            lost = {h.id for h in carry_file.dropped}
            carried.extend(h.id for h in hunks if h.id not in lost)
            dropped.extend(h.id for h in hunks if h.id in lost)
            for hunk in carry_file.applied:
                if hunk.origin == "host":
                    host.setdefault(change.path, []).extend(hunk.new_lines)
        return cls(base, start, host), CarryReport(tuple(carried), tuple(dropped))

    @property
    def base(self) -> TurnBase:
        return self._base

    # -- reads --------------------------------------------------------------

    def paths(self) -> list[str]:
        """Every path the Turn can see: its Source files as staged, and the Assets."""
        known = {*self._base.files, *self._start, *self._staged}
        existing = {path for path in known if self._current(path) is not None}
        return sorted(existing | self._assets)

    def read(self, path: str) -> StagedText:
        """The text of a Source file as staged; ToolError if it cannot be read."""
        rel = _checked(path, for_write=False)
        if rel in self._assets:
            raise _binary(path)
        text = self._current(rel)
        if text is None:
            raise ToolError("not_found", f"There is no file '{path}'.", path=path)
        return StagedText(rel, text, view_id(text))

    # -- writes -------------------------------------------------------------

    def write(self, path: str, text: str, *, by: By) -> StagedChange:
        """Replace the text of an existing Source file."""
        rel = _checked(path, for_write=True)
        if rel in self._assets:
            raise _binary(path)
        old = self._current(rel)
        if old is None:
            raise ToolError(
                "not_found", f"There is no file '{path}'; create it instead.", path=path
            )
        new = _checked_text(path, text, old)
        span = _span(old, new)
        self._set(rel, new, old, by)
        return StagedChange(rel, "modify", span)

    def create(self, path: str, text: str, *, by: By) -> StagedChange:
        """Create a Source file that does not exist."""
        rel = _checked(path, for_write=True)
        if rel in self._assets or self._current(rel) is not None:
            raise ToolError(
                "exists", f"'{path}' already exists; edit it instead of creating it.", path=path
            )
        new = _checked_text(path, text, None)
        count = len(split_lines(new))
        self._set(rel, new, None, by)
        return StagedChange(rel, "create", (1, count) if count else None)

    def delete(self, path: str, *, by: By) -> StagedChange:
        """Delete an existing Source file."""
        rel = _checked(path, for_write=True)
        if rel in self._assets:
            raise _binary(path)
        old = self._current(rel)
        if old is None:
            raise ToolError("not_found", f"There is no file '{path}'.", path=path)
        self._set(rel, None, old, by)
        return StagedChange(rel, "delete", None)

    def mark(self) -> int:
        """A counter that every effective change increments."""
        return self._changes

    def changed_since(self, mark: int) -> bool:
        return self._changes != mark

    # -- results ------------------------------------------------------------

    def tree(self) -> SourceTree:
        """The Proposal tree: every Source file overlaid with its staged text.

        RootFileMissing when the base has no Root file.
        """
        overlay: dict[str, str | None] = {path: self._current(path) for path in self._base.files}
        for path in {*self._start, *self._staged}:
            overlay[path] = self._current(path)
        return _tree(self._base, overlay)

    def files(self) -> list[StagedFile]:
        """Every path whose start or staged text differs from its base, by path."""
        out: list[StagedFile] = []
        for path in sorted({*self._start, *self._staged}):
            out.append(
                StagedFile(
                    path=path,
                    base=self._base.files.get(path),
                    start=self._start_text(path),
                    staged=self._current(path),
                    host_lines=tuple(self._host.get(path, ())),
                )
            )
        return out

    # -- internals ----------------------------------------------------------

    def _start_text(self, path: str) -> str | None:
        if path in self._start:
            return self._start[path]
        found = self._base.files.get(path)
        return found.text if found is not None else None

    def _current(self, path: str) -> str | None:
        if path in self._staged:
            return self._staged[path]
        return self._start_text(path)

    def _set(self, path: str, new: str | None, old: str | None, by: By) -> None:
        if new == old:
            return
        if new == self._start_text(path):
            self._staged.pop(path, None)
        else:
            self._staged[path] = new
        if by == "host" and new is not None:
            lines = self._host.setdefault(path, {})
            for line in _inserted(old, new):
                lines[line] = None
        self._changes += 1


@dataclass(frozen=True)
class _Carried:
    """One file of a carried Proposal: its start text, if it changed, and its Hunks' fate."""

    changed: bool
    text: str | None
    applied: tuple[Hunk, ...]
    dropped: tuple[Hunk, ...]


def _carry(base: TurnBase, op: str, path: str, new_text: str | None, hunks: list[Hunk]) -> _Carried:
    present = base.files.get(path)
    unchanged = _Carried(False, None, (), ())
    lost = _Carried(False, None, (), tuple(hunks))
    if op == "create":
        if present is not None or path in base.assets:
            return lost  # the user made a file there meanwhile
        return _Carried(True, new_text or "", tuple(hunks), ())
    if op == "delete":
        if present is None:  # gone already, unless it is no longer text
            return lost if path in base.assets else unchanged
        if split_lines(present.text) != list(hunks[0].old_lines):
            return lost
        return _Carried(True, None, tuple(hunks), ())
    if present is None:
        return lost
    lines = split_lines(present.text)
    placement = place(hunks, lines)
    applied = tuple(h for h, _ in placement.placed)
    text = join_lines(placement.apply(lines), like=present.text)
    return _Carried(text != present.text, text, applied, placement.stale)


def _tree(base: TurnBase, overlay: Mapping[str, str | None]) -> SourceTree:
    if base.root_file is None:
        raise RootFileMissing()
    eols: dict[str, Eol] = {path: f.eol for path, f in base.files.items()}
    for path in overlay:
        eols.setdefault(path, "lf")  # a created file is written with LF
    return source_tree(
        base.projects,
        base.project_id,
        overlay=overlay,
        eols=eols,
        root_file=base.root_file,
        engine=base.engine,
    )


def _checked(path: str, *, for_write: bool) -> str:
    try:
        return check_source_path(path, for_write=for_write)
    except PathDenied as exc:
        code: ToolErrorCode = "write_denied" if exc.code == "write_denied" else "path_denied"
        raise ToolError(code, f"'{path}': {exc.message}", path=path) from exc


def _binary(path: str) -> ToolError:
    return ToolError(
        "binary",
        f"'{path}' is not a text Source file; only its name and size can be seen.",
        path=path,
    )


def _checked_text(path: str, text: str, old: str | None) -> str:
    """`text` with CRLF as LF, after the size and control-character checks.

    The size limit refuses new text over 1 MiB, unless the file already is at
    least that large. Only lines the write puts in are checked for control
    characters, so a file that already holds one can still be edited.
    """
    new = text.replace("\r\n", "\n")
    try:
        size = len(new.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ToolError(
            "control_char",
            f"The text for '{path}' holds an unpaired surrogate. Nothing was changed.",
            path=path,
        ) from exc
    old_size = len(old.encode("utf-8", "surrogatepass")) if old is not None else 0
    if size > MAX_WRITE_BYTES and size > old_size:
        raise ToolError(
            "too_large",
            f"The text for '{path}' is over 1 MiB. Make a smaller change.",
            path=path,
        )
    if control_char(new) is not None:
        new_lines = split_lines(new)
        for number in _inserted_numbers(old, new):
            offset = control_char(new_lines[number - 1])
            if offset is not None:
                code = ord(new_lines[number - 1][offset])
                raise ToolError(
                    "control_char",
                    f"Line {number} of the text for '{path}' holds the control character "
                    f"U+{code:04X}. Nothing was changed.",
                    path=path,
                )
    return new


def _opcodes(
    old: str | None, new: str
) -> tuple[list[str], Sequence[tuple[str, int, int, int, int]]]:
    old_lines, new_lines = split_lines(old or ""), split_lines(new)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    return new_lines, matcher.get_opcodes()


def _inserted_numbers(old: str | None, new: str) -> list[int]:
    """The 1-based numbers of the lines of `new` that are not lines of `old`."""
    _, opcodes = _opcodes(old, new)
    return [j + 1 for tag, _, _, j1, j2 in opcodes if tag != "equal" for j in range(j1, j2)]


def _inserted(old: str | None, new: str) -> list[str]:
    """The lines of `new` that a diff from `old` shows as put in."""
    new_lines, opcodes = _opcodes(old, new)
    return [new_lines[j] for tag, _, _, j1, j2 in opcodes if tag != "equal" for j in range(j1, j2)]


def _span(old: str, new: str) -> tuple[int, int] | None:
    """The first and last changed line of `new`; see StagedChange."""
    _, opcodes = _opcodes(old, new)
    changed = [op for op in opcodes if op[0] != "equal"]
    if not changed:
        return None
    first, last = changed[0], changed[-1]
    return first[3] + 1, max(last[4], first[3])
