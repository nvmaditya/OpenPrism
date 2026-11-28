"""Proposals: Hunks, rebase, line maps and the desk that resolves them (agent design §6).

A Turn's staged edits leave staging only as a Proposal: per file, the Hunks
that turn its base text into the staged text. `compute` builds one from the
staged files; `ProposalDesk` keeps the open Proposal of each Project under
`state/<project_id>/proposals/` and resolves it hunk by hunk. Only `resolve`
writes agent text into a Project folder (invariant I1), and only where a
Hunk's original lines are found exactly (I5).

Lines are LF text without terminators. A text's lines are its LF-separated
pieces, so "a\\nb\\n" and "a\\nb" both have two lines; whether a file ends
with a line break is kept from the file a Hunk is applied to.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

from prismtex.agent.errors import AgentError, ProjectBusy, ProposalClosed, ProposalNotFound
from prismtex.agent.threads import Resolution
from prismtex.agent.types import (
    FileState,
    ProfileName,
    ResolveRequest,
    ResolveResult,
    StaleHunk,
    StaleReason,
    TurnStatus,
)
from prismtex.agent.verify import Verification
from prismtex.errors import FileNotFound, VersionConflict
from prismtex.history import Checkpoint, History, HistoryError
from prismtex.layout import DataLayout
from prismtex.workspace import ProjectStore, decode_text, detect_eol, encode_text

if TYPE_CHECKING:
    from prismtex.agent.staging import StagedFile

__all__ = [
    "DESTRUCTIVE",
    "PROPOSAL_VERSION",
    "AlreadyApplied",
    "BaseSource",
    "ChangeNote",
    "Exact",
    "FileChange",
    "FileOp",
    "Hunk",
    "LineMap",
    "NoteKind",
    "Origin",
    "Placement",
    "Proposal",
    "ProposalDesk",
    "ProposalMeta",
    "Rebased",
    "Shifted",
    "Stale",
    "compute",
    "join_lines",
    "line_map",
    "make_hunk",
    "place",
    "rebase",
    "split_lines",
]

PROPOSAL_VERSION: Final = 1
CONTEXT_LINES: Final = 3

Origin = Literal["model", "host"]
FileOp = Literal["modify", "create", "delete"]
BaseSource = Literal["disk", "buffer"]
NoteKind = Literal[
    "heading_removed",
    "heading_renamed",
    "heading_added",
    "input_removed",
    "input_added",
    "label_removed",
    "package_added",
    "package_removed",
    "documentclass_changed",
    "preamble_changed",
    "file_created",
    "file_deleted",
    "not_included",
    "bib_entry_added",
    "wrapper_stripped",
    "equation_no_relation",
]
# Note kinds that block auto-accept (agent design §6.1, §6.6).
DESTRUCTIVE: Final[frozenset[NoteKind]] = frozenset(
    {
        "heading_removed",
        "heading_renamed",
        "input_removed",
        "label_removed",
        "package_removed",
        "documentclass_changed",
        "preamble_changed",
        "file_deleted",
        "bib_entry_added",
    }
)

_ORIGINS = frozenset(get_args(Origin))
_OPS = frozenset(get_args(FileOp))
_SOURCES = frozenset(get_args(BaseSource))
_NOTE_KINDS = frozenset(get_args(NoteKind))
_PROFILES = frozenset(get_args(ProfileName))


# -- lines ----------------------------------------------------------------------


def split_lines(text: str) -> list[str]:
    """The LF lines of `text`, without terminators; a final line break adds no line."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def join_lines(lines: Sequence[str], like: str) -> str:
    """Join lines, ending with a line break unless `like` is non-empty and lacks one.

    An empty last line always gets its line break; without it the line is lost.
    """
    if not lines:
        return ""
    body = "\n".join(lines)
    final = like == "" or like.endswith("\n") or lines[-1] == ""
    return body + "\n" if final else body


# -- records --------------------------------------------------------------------


@dataclass(frozen=True)
class Hunk:
    """One contiguous change the user accepts or rejects on its own (agent design §6.1).

    `old_start` is the 1-based first base line replaced, or for a pure
    insertion the line it follows (0 is the top). `before` and `after` hold up
    to 3 base lines around the change, for rebase. `id` is "h" plus 12 hex of
    sha256 over the path, the context and the old and new lines, with "-2",
    "-3" on a collision; it is assigned when the Hunk is computed and kept
    while its Proposal is open.
    """

    id: str
    path: str
    old_start: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]
    origin: Origin

    @property
    def index(self) -> int:
        """The 0-based index of the first old line, or of the insertion point."""
        return self.old_start - 1 if self.old_lines else self.old_start

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "old_start": self.old_start,
            "old_lines": list(self.old_lines),
            "new_lines": list(self.new_lines),
            "before": list(self.before),
            "after": list(self.after),
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Hunk:
        return cls(
            id=_str(data["id"]),
            path=_str(data["path"]),
            old_start=_int(data["old_start"]),
            old_lines=_strs(data["old_lines"]),
            new_lines=_strs(data["new_lines"]),
            before=_strs(data["before"]),
            after=_strs(data["after"]),
            origin=_choice(data["origin"], _ORIGINS),
        )


@dataclass(frozen=True)
class FileChange:
    """The Hunks of one file (agent design §6.1).

    `base_version` is the content version every Hunk is relative to (None for
    a create). Hunks are ordered by `old_start` and never overlap. `new_text`
    is the full proposed LF text (None for a delete); for a modify, `resolve`
    never writes it, it applies the Hunks to the file as it is then.
    """

    path: str
    op: FileOp
    base_version: str | None
    base_source: BaseSource
    hunks: tuple[Hunk, ...]
    new_text: str | None
    added: int
    removed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "op": self.op,
            "base_version": self.base_version,
            "base_source": self.base_source,
            "hunks": [h.to_dict() for h in self.hunks],
            "new_text": self.new_text,
            "added": self.added,
            "removed": self.removed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FileChange:
        return cls(
            path=_str(data["path"]),
            op=_choice(data["op"], _OPS),
            base_version=_optional_str(data["base_version"]),
            base_source=_choice(data["base_source"], _SOURCES),
            hunks=tuple(Hunk.from_dict(h) for h in _list(data["hunks"])),
            new_text=_optional_str(data["new_text"]),
            added=_int(data["added"]),
            removed=_int(data["removed"]),
        )


@dataclass(frozen=True)
class LineMap:
    """Where the unchanged lines of a file's base sit in its new text.

    Each run is (base_start, base_end, offset): base lines base_start to
    base_end (1-based, inclusive) are new lines base_start + offset onward.
    """

    path: str
    runs: tuple[tuple[int, int, int], ...]

    def to_new(self, base_line: int) -> int | None:
        """The new line of an unchanged base line; None if it was replaced or removed."""
        for start, end, offset in self.runs:
            if start <= base_line <= end:
                return base_line + offset
        return None

    def to_base(self, new_line: int) -> int | None:
        """The base line of an unchanged new line; None if the line is new."""
        for start, end, offset in self.runs:
            if start + offset <= new_line <= end + offset:
                return new_line - offset
        return None


@dataclass(frozen=True)
class ChangeNote:
    """A structural change worth naming in review (agent design §6.1, §7.6).

    `dangling_refs` counts, for label_removed, the \\ref and \\eqref still
    pointing at the label.
    """

    kind: NoteKind
    path: str
    line: int | None
    before: str | None
    after: str | None
    dangling_refs: int = 0
    origin: Origin = "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "before": self.before,
            "after": self.after,
            "dangling_refs": self.dangling_refs,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChangeNote:
        line = data["line"]
        return cls(
            kind=_choice(data["kind"], _NOTE_KINDS),
            path=_str(data["path"]),
            line=None if line is None else _int(line),
            before=_optional_str(data["before"]),
            after=_optional_str(data["after"]),
            dangling_refs=_int(data["dangling_refs"]),
            origin=_choice(data["origin"], _ORIGINS),
        )


@dataclass(frozen=True)
class Proposal:
    """The staged changes of one or more Turns, offered for review (agent design §6.1).

    `id` is the producing Turn's id; `turn_ids` the Turns whose Hunks it
    carries (provenance); `undecided` the Hunk ids not yet accepted or
    rejected. An open Proposal holds only undecided Hunks.
    """

    id: str
    project_id: str
    thread_id: str
    turn_ids: tuple[str, ...]
    supersedes: str | None
    model: str
    profile: ProfileName
    created_at: str
    files: tuple[FileChange, ...]
    notes: tuple[ChangeNote, ...]
    verification: Verification
    undecided: frozenset[str]
    auto_accept_eligible: bool

    def hunks(self) -> tuple[Hunk, ...]:
        """Every Hunk, in file order and then line order."""
        return tuple(h for change in self.files for h in change.hunks)

    def to_dict(self) -> dict[str, Any]:
        """The whole Proposal as JSON, `new_text` included, with `"v": 1`."""
        return {
            "v": PROPOSAL_VERSION,
            "id": self.id,
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "turn_ids": list(self.turn_ids),
            "supersedes": self.supersedes,
            "model": self.model,
            "profile": self.profile,
            "created_at": self.created_at,
            "files": [change.to_dict() for change in self.files],
            "notes": [note.to_dict() for note in self.notes],
            "verification": self.verification.to_dict(),
            "undecided": [h.id for h in self.hunks() if h.id in self.undecided],
            "auto_accept_eligible": self.auto_accept_eligible,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Proposal:
        """Build from `to_dict` output; ValueError when it is not a v1 Proposal."""
        if data.get("v") != PROPOSAL_VERSION:
            raise ValueError(f"not a v{PROPOSAL_VERSION} Proposal")
        try:
            return cls(
                id=_str(data["id"]),
                project_id=_str(data["project_id"]),
                thread_id=_str(data["thread_id"]),
                turn_ids=_strs(data["turn_ids"]),
                supersedes=_optional_str(data["supersedes"]),
                model=_str(data["model"]),
                profile=_choice(data["profile"], _PROFILES),
                created_at=_str(data["created_at"]),
                files=tuple(FileChange.from_dict(c) for c in _list(data["files"])),
                notes=tuple(ChangeNote.from_dict(n) for n in _list(data["notes"])),
                verification=Verification.from_dict(data["verification"]),
                undecided=frozenset(_strs(data["undecided"])),
                auto_accept_eligible=_bool(data["auto_accept_eligible"]),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("not a complete Proposal") from exc


@dataclass(frozen=True)
class ProposalMeta:
    """What `compute` needs to know about the Turn that staged the changes."""

    turn_id: str
    project_id: str
    thread_id: str
    model: str
    profile: ProfileName
    created_at: str
    status: TurnStatus


@dataclass(frozen=True)
class Exact:
    """The Hunk applies where it was: `start` is its `old_start`."""

    start: int


@dataclass(frozen=True)
class Shifted:
    """The Hunk applies elsewhere: `start` is its new `old_start`."""

    start: int


@dataclass(frozen=True)
class AlreadyApplied:
    """The Hunk's new lines are there already: `start` is where they begin.

    For a pure deletion it is the line the removed lines followed.
    """

    start: int


@dataclass(frozen=True)
class Stale:
    """The Hunk's lines are no longer where it can find them; it is not applied."""

    reason: Literal["context_changed"] = "context_changed"


Rebased = Exact | Shifted | AlreadyApplied | Stale


# -- Hunks ------------------------------------------------------------------------


def make_hunk(
    path: str,
    base_lines: Sequence[str],
    old_start: int,
    old_lines: tuple[str, ...],
    new_lines: tuple[str, ...],
    *,
    origin: Origin,
) -> Hunk:
    """A Hunk over `base_lines`, with up to 3 lines of context on each side.

    Raises ValueError when `old_lines` are not at `old_start` in `base_lines`.
    """
    at = old_start - 1 if old_lines else old_start
    end = at + len(old_lines)
    if at < 0 or end > len(base_lines) or tuple(base_lines[at:end]) != old_lines:
        raise ValueError(f"the old lines are not at line {old_start} of {path}")
    before = tuple(base_lines[max(0, at - CONTEXT_LINES) : at])
    after = tuple(base_lines[end : end + CONTEXT_LINES])
    return Hunk(
        id=_hunk_id(path, before, old_lines, new_lines, after),
        path=path,
        old_start=old_start,
        old_lines=old_lines,
        new_lines=new_lines,
        before=before,
        after=after,
        origin=origin,
    )


def _hunk_id(
    path: str,
    before: Sequence[str],
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    after: Sequence[str],
) -> str:
    payload = json.dumps([path, list(before), list(old_lines), list(new_lines), list(after)])
    return "h" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def rebase(hunk: Hunk, current: Sequence[str]) -> Rebased:
    """Where `hunk` applies in `current`, trying each rung in order (agent design §6.4).

    1. Exact: the old lines sit at `old_start`.
    2. Shifted: `before + old_lines + after` is found exactly once.
    3. Old lines alone: at least 2 lines long and found exactly once.
    4. Already applied: `before + new_lines + after` is found exactly once.
    5. Otherwise Stale("context_changed").

    A pure insertion anchors on `before` and `after` being adjacent: at
    `old_start` for rung 1, and found exactly once for rung 2. Nothing fuzzier
    is ever applied (invariant I5).
    """
    lines = list(current)
    old, new = list(hunk.old_lines), list(hunk.new_lines)
    before, after = list(hunk.before), list(hunk.after)
    if old:
        at = hunk.old_start - 1
        if 0 <= at and lines[at : at + len(old)] == old:
            return Exact(start=hunk.old_start)
        found = _find(lines, before + old + after)
        if len(found) == 1:
            return Shifted(start=found[0] + len(before) + 1)
        if len(old) >= 2:
            found = _find(lines, old)
            if len(found) == 1:
                return Shifted(start=found[0] + 1)
    else:
        if _anchored(lines, hunk.old_start, before, after):
            return Exact(start=hunk.old_start)
        found = _find(lines, before + after)
        if len(found) == 1:
            return Shifted(start=found[0] + len(before))
    applied = before + new + after
    if applied:
        found = _find(lines, applied)
        if len(found) == 1:
            at = found[0] + len(before)
            return AlreadyApplied(start=at + 1 if new else at)
    elif not lines:
        return AlreadyApplied(start=0)  # a Hunk that empties a file, on an empty file
    return Stale()


def _anchored(lines: list[str], at: int, before: list[str], after: list[str]) -> bool:
    """True when `before` ends and `after` starts at index `at` (a pure insertion)."""
    if not before and not after:
        return at == 0 and not lines  # no context: the base file was empty
    if at - len(before) < 0 or at + len(after) > len(lines):
        return False
    return lines[at - len(before) : at] == before and lines[at : at + len(after)] == after


def _find(lines: list[str], needle: list[str]) -> list[int]:
    """Every index where `needle` starts in `lines`; an empty needle is never found."""
    if not needle or len(needle) > len(lines):
        return []
    first, size = needle[0], len(needle)
    return [
        i
        for i in range(len(lines) - size + 1)
        if lines[i] == first and lines[i : i + size] == needle
    ]


@dataclass(frozen=True)
class Placement:
    """Where a set of Hunks applies in one text (see `place`).

    `placed` pairs each Hunk that applies with its index (`Hunk.index` in the
    text placed onto); none of them overlap. `already` holds Hunks whose new
    lines are there already, `stale` the ones that do not apply.
    """

    placed: tuple[tuple[Hunk, int], ...]
    already: tuple[Hunk, ...]
    stale: tuple[Hunk, ...]

    def apply(self, lines: Sequence[str]) -> list[str]:
        """`lines` with every placed Hunk applied."""
        return _splice(list(lines), self.placed, lambda line: line)


def place(
    hunks: Iterable[Hunk],
    lines: Sequence[str],
    *,
    taken: Sequence[tuple[Hunk, int]] = (),
) -> Placement:
    """Rebase each Hunk onto `lines`, in order, keeping only non-overlapping ones.

    A Hunk that overlaps one placed before it, or one in `taken`, is stale.
    """
    current = list(lines)
    placed: list[tuple[Hunk, int]] = []
    already: list[Hunk] = []
    stale: list[Hunk] = []
    for hunk in hunks:
        result = rebase(hunk, current)
        if isinstance(result, Exact | Shifted):
            at = result.start - 1 if hunk.old_lines else result.start
            if any(_overlap(hunk, at, other, spot) for other, spot in (*taken, *placed)):
                stale.append(hunk)
            else:
                placed.append((hunk, at))
        elif isinstance(result, AlreadyApplied):
            already.append(hunk)
        else:
            stale.append(hunk)
    return Placement(placed=tuple(placed), already=tuple(already), stale=tuple(stale))


def _overlap(a: Hunk, a_at: int, b: Hunk, b_at: int) -> bool:
    a_end, b_end = a_at + len(a.old_lines), b_at + len(b.old_lines)
    if a.old_lines and b.old_lines:
        return a_at < b_end and b_at < a_end
    if a.old_lines:
        return a_at < b_at < a_end
    if b.old_lines:
        return b_at < a_at < b_end
    return a_at == b_at


def _splice[T](
    items: list[T], placed: Sequence[tuple[Hunk, int]], make: Callable[[str], T]
) -> list[T]:
    """Replace each placed Hunk's old lines with its new lines, bottom-up.

    At the same index a replacement is applied before an insertion, so the
    inserted lines land before the replaced ones.
    """
    out = list(items)
    for hunk, at in sorted(placed, key=lambda p: (p[1], bool(p[0].old_lines)), reverse=True):
        out[at : at + len(hunk.old_lines)] = [make(line) for line in hunk.new_lines]
    return out


def _shift(at: int, applied: Sequence[tuple[Hunk, int]]) -> int:
    """Index `at` of a text after the non-overlapping `applied` Hunks were applied to it."""
    delta = 0
    for hunk, spot in applied:
        if spot + len(hunk.old_lines) <= at:
            delta += len(hunk.new_lines) - len(hunk.old_lines)
    return at + delta


# -- compute ----------------------------------------------------------------------


def compute(
    files: Sequence[StagedFile],
    notes: Sequence[ChangeNote],
    verification: Verification,
    meta: ProposalMeta,
    carried: Proposal | None,
) -> Proposal | None:
    """The Proposal of a Turn's staged files; None when nothing differs from the start text.

    One Hunk per non-equal opcode of difflib.SequenceMatcher(autojunk=False)
    over the base and staged lines. A Hunk is "host" when every one of its new
    lines is among the lines host writes put in its file, or when the carried
    Proposal had it as a host Hunk. A create is one Hunk at line 0, a delete
    one Hunk with every base line. A change to whether a file ends with a line
    break alone is not a change.
    """
    if not any(_differs(f.start, f.staged) for f in files):
        return None
    carried_origin: dict[str, Origin] = {h.id: h.origin for h in carried.hunks()} if carried else {}
    seen: dict[str, int] = {}
    changes: list[FileChange] = []
    for staged in sorted(files, key=lambda f: f.path):
        change = _file_change(staged, carried_origin, seen)
        if change is not None:
            changes.append(change)
    undecided = frozenset(h.id for change in changes for h in change.hunks)
    eligible = (
        meta.status == "completed"
        and verification.verdict == "clean"
        and not any(note.kind in DESTRUCTIVE for note in notes)
        and all(change.base_source == "disk" for change in changes)
    )
    turn_ids = tuple(dict.fromkeys([*(carried.turn_ids if carried else ()), meta.turn_id]))
    return Proposal(
        id=meta.turn_id,
        project_id=meta.project_id,
        thread_id=meta.thread_id,
        turn_ids=turn_ids,
        supersedes=carried.id if carried else None,
        model=meta.model,
        profile=meta.profile,
        created_at=meta.created_at,
        files=tuple(changes),
        notes=tuple(notes),
        verification=verification,
        undecided=undecided,
        auto_accept_eligible=eligible,
    )


def _differs(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return split_lines(a) != split_lines(b)


def _file_change(
    staged: StagedFile, carried_origin: Mapping[str, Origin], seen: dict[str, int]
) -> FileChange | None:
    path, base = staged.path, staged.base
    host = frozenset(staged.host_lines)

    def finish(hunk: Hunk) -> Hunk:
        count = seen.get(hunk.id, 0) + 1
        seen[hunk.id] = count
        hunk_id = hunk.id if count == 1 else f"{hunk.id}-{count}"
        by_host = bool(hunk.new_lines) and all(line in host for line in hunk.new_lines)
        origin = carried_origin.get(hunk_id, "host" if by_host else "model")
        return replace(hunk, id=hunk_id, origin=origin)

    if base is None:
        if staged.staged is None:
            return None
        new = tuple(split_lines(staged.staged))
        hunk = finish(make_hunk(path, (), 0, (), new, origin="model"))
        return FileChange(path, "create", None, "disk", (hunk,), staged.staged, len(new), 0)
    base_lines = split_lines(base.text)
    if staged.staged is None:
        old = tuple(base_lines)
        hunk = finish(make_hunk(path, base_lines, 1 if old else 0, old, (), origin="model"))
        return FileChange(path, "delete", base.version, base.source, (hunk,), None, 0, len(old))
    staged_lines = split_lines(staged.staged)
    if staged_lines == base_lines:
        return None
    matcher = difflib.SequenceMatcher(None, base_lines, staged_lines, autojunk=False)
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old, new = tuple(base_lines[i1:i2]), tuple(staged_lines[j1:j2])
        start = i1 + 1 if old else i1
        hunks.append(finish(make_hunk(path, base_lines, start, old, new, origin="model")))
    return FileChange(
        path=path,
        op="modify",
        base_version=base.version,
        base_source=base.source,
        hunks=tuple(hunks),
        new_text=staged.staged,
        added=sum(len(h.new_lines) for h in hunks),
        removed=sum(len(h.old_lines) for h in hunks),
    )


def line_map(change: FileChange) -> LineMap:
    """The unchanged runs of a computed FileChange; empty for a create or a delete."""
    if change.op != "modify" or change.new_text is None:
        return LineMap(change.path, ())
    hunks = sorted(change.hunks, key=lambda h: (h.index, bool(h.old_lines)))
    new_count = len(split_lines(change.new_text))
    base_count = (
        new_count - sum(len(h.new_lines) for h in hunks) + sum(len(h.old_lines) for h in hunks)
    )
    runs: list[tuple[int, int, int]] = []
    base_line, new_line = 1, 1  # the next unconsumed line of each side
    for hunk in hunks:
        length = hunk.index - (base_line - 1)
        if length > 0:
            runs.append((base_line, hunk.index, new_line - base_line))
            new_line += length
        new_line += len(hunk.new_lines)
        base_line = hunk.index + len(hunk.old_lines) + 1
    if base_line <= base_count:
        runs.append((base_line, base_count, new_line - base_line))
    return LineMap(change.path, tuple(runs))


# -- the desk ---------------------------------------------------------------------

# Proposal ids are Turn ids and name files, so they are single safe path segments.
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CLOSED_FILE: Final = "closed.txt"
_CLOSED_KEEP: Final = 200
_TEMP_PREFIX: Final = ".prismtex-"
_TEMP_SUFFIX: Final = ".tmp"
# A reader can hold a file open on Windows without delete sharing, so
# os.replace and unlink raise PermissionError; they are retried, 50 ms apart.
_RETRIES: Final = 10
_RETRY_DELAY_S: Final = 0.05


@dataclass
class _Applied:
    """What one `resolve` did to the files, before the Proposal is reduced."""

    applied: list[str] = field(default_factory=list[str])
    stale: list[StaleHunk] = field(default_factory=list[StaleHunk])
    files: list[FileState] = field(default_factory=list[FileState])
    # path -> (written LF text, its version, the remaining Hunks placed in it)
    rewritten: dict[str, tuple[str, str, tuple[tuple[Hunk, int], ...]]] = field(
        default_factory=dict[str, tuple[str, str, tuple[tuple[Hunk, int], ...]]]
    )


class ProposalDesk:
    """The open Proposal of each Project, and `resolve` (agent design §6.3 to §6.5).

    The open Proposal is `state/<project_id>/proposals/<proposal_id>.json`
    (`"v": 1`), replaced atomically and deleted when it closes or is
    superseded. The ids of closed Proposals are kept in `closed.txt` beside
    it (the newest 200), so `get` can tell a closed id from an unknown one.
    `active_turn(project_id)` names the Project's active Turn, if any;
    `on_resolved(project_id, thread_id, resolution)` runs after each resolve
    that decided something.
    """

    def __init__(
        self,
        data_dir: Path,
        projects: ProjectStore,
        history: History,
        *,
        active_turn: Callable[[str], str | None],
        on_resolved: Callable[[str, str, Resolution], None],
    ) -> None:
        self._layout = DataLayout(data_dir)
        self._projects = projects
        self._history = history
        self._active_turn = active_turn
        self._on_resolved = on_resolved
        self._locks: dict[str, asyncio.Lock] = {}

    # -- reading ------------------------------------------------------------

    def open_proposal(self, project_id: str) -> Proposal | None:
        """The Project's open Proposal, or None."""
        return self._open(self._folder(project_id), project_id)

    def get(self, project_id: str, proposal_id: str) -> Proposal:
        """The open Proposal with this id.

        ProposalClosed (with the open Proposal's id) when it was resolved or
        superseded; ProposalNotFound when there was never such a Proposal.
        """
        folder = self._folder(project_id)
        current = self._open(folder, project_id)
        if current is not None and current.id == proposal_id:
            return current
        if _ID.fullmatch(proposal_id) and (
            proposal_id in _read_closed(folder) or (folder / f"{proposal_id}.json").is_file()
        ):
            raise ProposalClosed(proposal_id, current.id if current else None)
        raise ProposalNotFound(proposal_id)

    # -- writing ------------------------------------------------------------

    def publish(self, proposal: Proposal) -> None:
        """Make `proposal` the open Proposal of its Project, superseding any other.

        A Proposal with no undecided Hunk (a Turn that only took back carried
        changes) closes the previous one and is not stored.
        """
        folder = self._folder(proposal.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        others = [p.id for p in self._stored(folder, proposal.project_id) if p.id != proposal.id]
        if not proposal.undecided:
            self._close(folder, [*others, proposal.id])
            return
        _check_id(proposal.id)
        if proposal.id in _read_closed(folder):
            self._close(folder, [], reopen=proposal.id)
        _write_json(folder / f"{proposal.id}.json", proposal.to_dict())
        self._close(folder, others)

    async def resolve(
        self, request: ResolveRequest, *, by: Literal["user", "auto"]
    ) -> ResolveResult:
        """Accept and reject Hunks of the open Proposal (agent design §6.4, §6.5).

        Refused with ProjectBusy while a Turn is active, unless `by` is
        "auto" (auto-accept runs inside that Turn's finalization). Accepted
        Hunks are applied where their old lines are found exactly; the others
        go stale and stay undecided. Writing is framed by a `save` Checkpoint
        before and one `accept` (or `auto_accept`) Checkpoint after.
        """
        project_id = request.project_id
        async with self._lock(project_id):
            proposal = self.get(project_id, request.proposal_id)
            if by != "auto":
                active = self._active_turn(project_id)
                if active is not None:
                    raise ProjectBusy(active)
            accept, reject = _decisions(proposal, request)
            if not accept and not reject:
                return _result(proposal, _Applied(), (), None, proposal)

            if accept:
                await self._checkpoint(
                    project_id, "save", f"Changes before accepting proposal {proposal.id}"
                )
            done = await asyncio.to_thread(
                self._apply, proposal, accept, dict(request.base_versions)
            )
            order = {h.id: i for i, h in enumerate(proposal.hunks())}
            done.stale.sort(key=lambda s: order[s.hunk_id])
            checkpoint_id = None
            if done.files:
                checkpoint = await self._checkpoint(
                    project_id,
                    "auto_accept" if by == "auto" else "accept",
                    _accept_message(proposal, done),
                    trailers={
                        "Proposal": proposal.id,
                        "Turns": ",".join(proposal.turn_ids),
                        "Model": proposal.model,
                        "Profile": proposal.profile,
                        "Hunks": ",".join(done.applied),
                    },
                )
                checkpoint_id = None if checkpoint is None else checkpoint.id

            rejected = tuple(h.id for h in proposal.hunks() if h.id in reject)
            reduced = _reduce(proposal, done, reject)
            folder = self._folder(project_id)
            still_open = self._open(folder, project_id)
            if still_open is not None and still_open.id == proposal.id:
                if reduced.undecided:
                    _write_json(folder / f"{proposal.id}.json", reduced.to_dict())
                else:
                    self._close(folder, [proposal.id])
            resolution = Resolution(
                proposal_id=proposal.id,
                at=_now(),
                applied=tuple(done.applied),
                rejected=rejected,
                stale=tuple(s.hunk_id for s in done.stale),
            )
            self._on_resolved(project_id, proposal.thread_id, resolution)
            return _result(proposal, done, rejected, checkpoint_id, reduced)

    # -- internals ------------------------------------------------------------

    def _folder(self, project_id: str) -> Path:
        self._projects.get(project_id)  # validates the id: ProjectNotFound
        return self._layout.state(project_id) / "proposals"

    def _lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = self._locks[project_id] = asyncio.Lock()
        return lock

    def _stored(self, folder: Path, project_id: str) -> list[Proposal]:
        """Every readable stored Proposal that is not closed; a broken file is skipped."""
        if not folder.is_dir():
            return []
        closed = _read_closed(folder)
        found: list[Proposal] = []
        for path in sorted(folder.glob("*.json")):
            if not _ID.fullmatch(path.stem) or path.stem in closed:
                continue
            try:
                proposal = Proposal.from_dict(json.loads(path.read_bytes()))
            except (OSError, ValueError):
                continue
            if proposal.id == path.stem and proposal.project_id == project_id:
                found.append(proposal)
        return found

    def _open(self, folder: Path, project_id: str) -> Proposal | None:
        # Two stored Proposals only exist after a crash inside `publish`: the
        # newer one is open.
        stored = self._stored(folder, project_id)
        return max(stored, key=lambda p: (p.created_at, p.id)) if stored else None

    def _close(self, folder: Path, ids: Sequence[str], *, reopen: str | None = None) -> None:
        """Record `ids` as closed, then delete their files; `reopen` leaves the record."""
        if not ids and reopen is None:
            return
        closed = [i for i in _read_closed(folder) if i not in ids and i != reopen] + list(ids)
        folder.mkdir(parents=True, exist_ok=True)
        _write_bytes(folder / _CLOSED_FILE, "".join(f"{i}\n" for i in closed[-_CLOSED_KEEP:]))
        for proposal_id in ids:
            if _ID.fullmatch(proposal_id):
                _unlink(folder / f"{proposal_id}.json")

    async def _checkpoint(
        self,
        project_id: str,
        reason: Literal["save", "accept", "auto_accept"],
        message: str,
        *,
        trailers: Mapping[str, str] | None = None,
    ) -> Checkpoint | None:
        # A History failure never blocks an accept: the result then carries
        # no checkpoint id.
        try:
            return await self._history.checkpoint(
                project_id, reason=reason, message=message, trailers=trailers
            )
        except HistoryError:
            return None

    def _apply(
        self, proposal: Proposal, accept: frozenset[str], base_versions: Mapping[str, str]
    ) -> _Applied:
        """Write the accepted Hunks, file by file (agent design §6.4 step 2)."""
        done = _Applied()
        for change in proposal.files:
            accepted = [h for h in change.hunks if h.id in accept]
            if not accepted:
                continue
            if change.op == "create":
                self._apply_create(proposal.project_id, change, accepted, base_versions, done)
            elif change.op == "delete":
                self._apply_delete(proposal.project_id, change, accepted, base_versions, done)
            else:
                self._apply_modify(proposal.project_id, change, accepted, base_versions, done)
        return done

    def _current(self, project_id: str, path: str) -> tuple[bytes, str] | None:
        try:
            return self._projects.read_bytes(project_id, path)
        except FileNotFound:
            return None

    def _apply_create(
        self,
        project_id: str,
        change: FileChange,
        accepted: list[Hunk],
        base_versions: Mapping[str, str],
        done: _Applied,
    ) -> None:
        if change.path in base_versions:
            current = self._current(project_id, change.path)
            if (None if current is None else current[1]) != base_versions[change.path]:
                _stale(done, accepted, "version_mismatch")
                return
        try:
            written = self._projects.write_new(project_id, change.path, change.new_text or "")
        except VersionConflict:
            _stale(done, accepted, "file_exists")
            return
        done.applied.extend(h.id for h in accepted)
        done.files.append(FileState(change.path, written.version, written.text))

    def _apply_delete(
        self,
        project_id: str,
        change: FileChange,
        accepted: list[Hunk],
        base_versions: Mapping[str, str],
        done: _Applied,
    ) -> None:
        current = self._current(project_id, change.path)
        if change.path in base_versions and (
            None if current is None else current[1]
        ) != base_versions.get(change.path):
            _stale(done, accepted, "version_mismatch")
            return
        if current is None:  # gone already: what the Hunk asked for
            done.applied.extend(h.id for h in accepted)
            return
        data, version = current
        text = _decode(data)
        if text is None or split_lines(text) != list(accepted[0].old_lines):
            _stale(done, accepted, "context_changed")
            return
        try:
            self._projects.delete_file(project_id, change.path, base_version=version)
        except VersionConflict:
            _stale(done, accepted, "version_mismatch")
            return
        done.applied.extend(h.id for h in accepted)
        done.files.append(FileState(change.path, None, None))

    def _apply_modify(
        self,
        project_id: str,
        change: FileChange,
        accepted: list[Hunk],
        base_versions: Mapping[str, str],
        done: _Applied,
    ) -> None:
        current = self._current(project_id, change.path)
        if current is None:
            _stale(done, accepted, "file_missing")
            return
        data, version = current
        if change.path in base_versions and base_versions[change.path] != version:
            _stale(done, accepted, "version_mismatch")
            return
        text = _decode(data)
        if text is None:
            _stale(done, accepted, "context_changed")
            return
        lines = split_lines(text)
        placement = place(accepted, lines)
        if placement.placed:
            try:
                written_text, written_version = self._write(
                    project_id, change.path, data, version, text, placement.placed
                )
            except VersionConflict:  # a save landed between the read and the write
                kept = [h for h, _ in placement.placed] + list(placement.already)
                _stale(done, kept, "version_mismatch")
                _stale(done, placement.stale, "context_changed")
                return
            # The Hunks still undecided, placed in the text before the write
            # and moved to where they sit in the text written.
            accepted_ids = {h.id for h in accepted}
            rest = [h for h in change.hunks if h.id not in accepted_ids or h in placement.stale]
            others = place(rest, lines, taken=placement.placed)
            moved = tuple((h, _shift(at, placement.placed)) for h, at in others.placed)
            done.rewritten[change.path] = (written_text, written_version, moved)
            done.files.append(FileState(change.path, written_version, written_text))
        applied = {h.id for h, _ in placement.placed} | {h.id for h in placement.already}
        done.applied.extend(h.id for h in accepted if h.id in applied)
        _stale(done, placement.stale, "context_changed")

    def _write(
        self,
        project_id: str,
        path: str,
        data: bytes,
        version: str,
        text: str,
        placed: Sequence[tuple[Hunk, int]],
    ) -> tuple[str, str]:
        """Write `placed` into the file read as `data`; (new LF text, new version).

        A file whose bytes are its LF text with one line ending goes through
        ProjectStore.write. A file with mixed line endings is rebuilt line by
        line, every line it keeps with its own ending, and written with
        replace_bytes, so only the changed lines differ.
        """
        eol = detect_eol(data)
        if encode_text(text, eol) == data:
            new_text = join_lines(_splice(split_lines(text), placed, lambda s: s), like=text)
            written = self._projects.write(project_id, path, new_text, base_version=version)
            return written.text, written.version
        new_data = _rewrite_mixed(data.decode("utf-8"), placed, "\r\n" if eol == "crlf" else "\n")
        new_version = self._projects.replace_bytes(
            project_id, path, new_data, expected_version=version
        )
        return new_data.decode("utf-8").replace("\r\n", "\n"), new_version


def _rewrite_mixed(raw: str, placed: Sequence[tuple[Hunk, int]], dominant: str) -> bytes:
    """Apply `placed` to raw text, keeping each kept line's own line ending."""
    pieces = raw.split("\n")
    pairs: list[tuple[str, str]] = []
    for i, piece in enumerate(pieces):
        if i == len(pieces) - 1:
            if piece:
                pairs.append((piece, ""))
        elif piece.endswith("\r"):
            pairs.append((piece[:-1], "\r\n"))
        else:
            pairs.append((piece, "\n"))
    final = pairs[-1][1] if pairs else ""
    out = _splice(pairs, placed, lambda line: (line, dominant))
    fixed = [(line, ending or dominant) for line, ending in out[:-1]]
    if out:
        last = out[-1][0]
        fixed.append((last, final or (dominant if last == "" else "")))
    return "".join(line + ending for line, ending in fixed).encode("utf-8")


def _decode(data: bytes) -> str | None:
    try:
        return decode_text(data)[0]
    except UnicodeDecodeError:
        return None


def _stale(done: _Applied, hunks: Iterable[Hunk], reason: StaleReason) -> None:
    done.stale.extend(StaleHunk(h.id, h.path, reason) for h in hunks)


def _decisions(
    proposal: Proposal, request: ResolveRequest
) -> tuple[frozenset[str], frozenset[str]]:
    """The accepted and rejected ids; AgentError(invalid_request) for bad ones (§6.4 step 1)."""
    undecided = frozenset(h.id for h in proposal.hunks() if h.id in proposal.undecided)
    accept = undecided if request.accept == "all" else frozenset(request.accept)
    reject = undecided if request.reject == "all" else frozenset(request.reject)
    unknown = (accept | reject) - undecided
    if unknown:
        raise AgentError(
            "invalid_request",
            "These hunks are not undecided hunks of this proposal: "
            + ", ".join(sorted(unknown))
            + ".",
            field="accept" if unknown & accept else "reject",
        )
    if accept & reject:
        raise AgentError(
            "invalid_request", "A hunk cannot be both accepted and rejected.", field="reject"
        )
    return accept, reject


def _reduce(proposal: Proposal, done: _Applied, reject: frozenset[str]) -> Proposal:
    """The Proposal after a resolve: decided Hunks removed, written files rebased.

    A file the call wrote becomes the base of its remaining Hunks: they move
    to where they sit in the written text, with their context from it and
    their ids kept. A remaining Hunk that no longer applies there is kept as
    it was and is left out of `new_text`.
    """
    decided = set(done.applied) | set(reject)
    files: list[FileChange] = []
    for change in proposal.files:
        remaining = [h for h in change.hunks if h.id not in decided]
        if not remaining:
            continue
        if len(remaining) == len(change.hunks):
            files.append(change)
            continue
        rewritten = done.rewritten.get(change.path)
        if rewritten is None:
            removed = [h for h in change.hunks if h.id in decided]
            files.append(
                replace(
                    change,
                    hunks=tuple(remaining),
                    new_text=_unapply(change, removed),
                    added=sum(len(h.new_lines) for h in remaining),
                    removed=sum(len(h.old_lines) for h in remaining),
                )
            )
            continue
        text, version, moved = rewritten
        lines = split_lines(text)
        positions = {h.id: at for h, at in moved}
        hunks: list[Hunk] = []
        placed: list[tuple[Hunk, int]] = []
        for hunk in remaining:
            at = positions.get(hunk.id)
            if at is None:
                hunks.append(hunk)
                continue
            end = at + len(hunk.old_lines)
            updated = replace(
                hunk,
                old_start=at + 1 if hunk.old_lines else at,
                before=tuple(lines[max(0, at - CONTEXT_LINES) : at]),
                after=tuple(lines[end : end + CONTEXT_LINES]),
            )
            hunks.append(updated)
            placed.append((updated, at))
        hunks.sort(key=lambda h: (h.index, bool(h.old_lines)))
        files.append(
            FileChange(
                path=change.path,
                op="modify",
                base_version=version,
                base_source="disk",
                hunks=tuple(hunks),
                new_text=join_lines(_splice(lines, placed, lambda s: s), like=text),
                added=sum(len(h.new_lines) for h in hunks),
                removed=sum(len(h.old_lines) for h in hunks),
            )
        )
    undecided = frozenset(h.id for change in files for h in change.hunks) & proposal.undecided
    return replace(proposal, files=tuple(files), undecided=undecided)


def _unapply(change: FileChange, removed: Sequence[Hunk]) -> str | None:
    """`change.new_text` without the `removed` Hunks (their old lines put back)."""
    if change.new_text is None:
        return None
    lines = split_lines(change.new_text)
    wanted = {h.id for h in removed}
    spots: list[tuple[Hunk, int]] = []
    delta = 0
    for hunk in sorted(change.hunks, key=lambda h: (h.index, bool(h.old_lines))):
        if hunk.id in wanted:
            spots.append((hunk, hunk.index + delta))
        delta += len(hunk.new_lines) - len(hunk.old_lines)
    for hunk, at in reversed(spots):
        if lines[at : at + len(hunk.new_lines)] == list(hunk.new_lines):
            lines[at : at + len(hunk.new_lines)] = list(hunk.old_lines)
    return join_lines(lines, like=change.new_text)


def _result(
    proposal: Proposal,
    done: _Applied,
    rejected: tuple[str, ...],
    checkpoint_id: str | None,
    reduced: Proposal,
) -> ResolveResult:
    undecided = tuple(h.id for h in reduced.hunks() if h.id in reduced.undecided)
    return ResolveResult(
        proposal_id=proposal.id,
        applied=tuple(done.applied),
        rejected=rejected,
        stale=tuple(done.stale),
        undecided=undecided,
        files=tuple(sorted(done.files, key=lambda f: f.path)),
        checkpoint_id=checkpoint_id,
        closed=not undecided,
    )


def _accept_message(proposal: Proposal, done: _Applied) -> str:
    """ "Accept proposal t_...: 3 changes in main.tex", naming the files written."""
    count = len(done.applied)
    paths = sorted({f.path for f in done.files})
    where = (
        paths[0]
        if len(paths) == 1
        else f"{paths[0]} and {paths[1]}"
        if len(paths) == 2
        else f"{len(paths)} files"
    )
    return f"Accept proposal {proposal.id}: {count} change{'' if count == 1 else 's'} in {where}"


def _now() -> str:
    """RFC 3339 UTC with milliseconds, as in "2026-11-24T23:10:04.120Z"."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _check_id(proposal_id: str) -> None:
    if not _ID.fullmatch(proposal_id):
        raise ValueError(f"not a plain proposal id: {proposal_id!r}")


def _read_closed(folder: Path) -> list[str]:
    try:
        raw = (folder / _CLOSED_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line for line in raw.split("\n") if line]


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    _write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _write_bytes(path: Path, text: str) -> None:
    """Write `text` (UTF-8) to `path` atomically: a synced temp file, then os.replace."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(text.encode("utf-8", "surrogatepass"))
            out.flush()
            os.fsync(out.fileno())
        _retry(lambda: os.replace(tmp_name, path))
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _unlink(path: Path) -> None:
    _retry(lambda: path.unlink(missing_ok=True))


def _retry(action: Callable[[], None]) -> None:
    for attempt in range(_RETRIES):
        try:
            action()
            return
        except PermissionError:
            if attempt + 1 == _RETRIES:
                raise
            time.sleep(_RETRY_DELAY_S)


# -- JSON values ------------------------------------------------------------------


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return value


def _optional_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected a boolean, got {value!r}")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {type(value).__name__}")
    return value  # pyright: ignore[reportUnknownVariableType]


def _strs(value: object) -> tuple[str, ...]:
    return tuple(_str(item) for item in _list(value))


def _choice[T: str](value: object, allowed: frozenset[T]) -> T:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"unexpected value {value!r}")
    return value  # pyright: ignore[reportReturnType]
