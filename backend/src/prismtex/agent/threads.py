"""Threads: the conversations a Project's Turns belong to (agent design §2.2 `threads`, §5.4).

A Thread is one append-only JSONL file, `state/<project_id>/threads/<thread_id>.jsonl`.
Every line is one typed record with `"v": 1`:

- `thread`, first and once: `thread_id`, `created_at` and `title` (null until named).
- `title`: a rename, with `at` and `title`.
- `segment`: a transcript segment the Turn `turn_id` opened, with the Segment fields.
- `messages`: the messages the Turn `turn_id` added to the current segment, exactly as
  sent and received (`native` included).
- `turn`: a Turn record. It commits the `segment` and `messages` lines of its Turn,
  which `append_turn` writes just before it in the same write.
- `resolution`: what one `resolve` call decided.

Each append is one write followed by fsync. A line cut off by a crash is skipped
when reading, and the next append starts on a new line, so nothing written before
it is lost. Lines of an unknown type or version are skipped too.

Memory (agent design §5.4): records memory renders the last Turn records as
`<memory>`; transcript memory replays the current segment's messages, and a new
segment opens with `<earlier_turns>`, a deterministic summary of earlier Turns.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

from prismtex.agent.errors import ThreadNotFound
from prismtex.agent.providers import (
    Block,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from prismtex.agent.types import Outcome, ProfileName, TurnStatus, Verdict
from prismtex.layout import DataLayout

__all__ = [
    "ADDED_TEXT_CHARS",
    "DEFAULT_TITLE",
    "EARLIER_TURNS",
    "MEMORY_TURNS",
    "MESSAGE_CHARS",
    "THREAD_VERSION",
    "TITLE_CHARS",
    "RegionRef",
    "Resolution",
    "Segment",
    "SegmentOpening",
    "ThreadInfo",
    "ThreadMemory",
    "ThreadStore",
    "ThreadView",
    "TurnRecord",
    "earlier_turns",
    "render_records",
    "render_since",
]

THREAD_VERSION: Final = 1
MESSAGE_CHARS: Final = 500  # TurnRecord.message keeps the first 500 characters
ADDED_TEXT_CHARS: Final = 1_000  # TurnRecord.added_text keeps the first 1,000
TITLE_CHARS: Final = 120
DEFAULT_TITLE: Final = "New thread"
# Turn records rendered as <memory> per Profile, and summarized by <earlier_turns>.
MEMORY_TURNS: Final[Mapping[ProfileName, int]] = MappingProxyType({"guided": 3, "agentic": 6})
EARLIER_TURNS: Final = 12

SegmentOpening = Literal["new_thread", "model_switch", "prompt_version", "rollover"]

# Statuses after which the next Turn hears that the previous one was stopped (§5.4).
_STOPPED: Final[frozenset[TurnStatus]] = frozenset(
    {"cancelled", "failed", "budget_exhausted", "interrupted"}
)
_STATUSES = frozenset(get_args(TurnStatus))
_OUTCOMES = frozenset(get_args(Outcome))
_VERDICTS = frozenset(get_args(Verdict))
_PROFILES = frozenset(get_args(ProfileName))
_OPENINGS = frozenset(get_args(SegmentOpening))
_ROLES: frozenset[Literal["user", "assistant"]] = frozenset({"user", "assistant"})

# Project and Thread ids name files, so they are single safe path segments.
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# A file another handle holds open on Windows cannot be deleted; retried 50 ms apart.
_RETRIES: Final = 6
_RETRY_DELAY_S: Final = 0.05

# Rendering limits for one line of <memory> or <earlier_turns>.
_LINE_MESSAGE_CHARS: Final = 200
_LINE_SUMMARY_CHARS: Final = 300
_REPLY_CHARS: Final = 800
_ADDED_CHARS: Final = 300
_TITLE_FROM_MESSAGE_CHARS: Final = 60


# -- records ----------------------------------------------------------------------


@dataclass(frozen=True)
class RegionRef:
    """A span a Turn edited, in its proposed text: 1-based inclusive lines.

    `start_line == end_line + 1` marks an insertion point after `end_line`.
    """

    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class TurnRecord:
    """What a Thread remembers of one Turn (agent design §2.2 `threads`).

    `message` keeps the first 500 characters of the request. `targets` are
    where the Turn edited, in the proposed text. `added_text` holds up to
    1,000 characters the Turn added, so "that sentence" can mean exactly it.
    `proposal_id` names the Proposal the Turn produced, if any. `reply` and
    `verdict` complete what a Thread view shows (agent design §10.3).
    """

    turn_id: str
    at: str
    status: TurnStatus
    outcome: Outcome
    model: str
    profile: ProfileName
    message: str
    summary: str
    proposal_id: str | None
    targets: tuple[RegionRef, ...]
    added_text: str | None
    reply: str = ""
    verdict: Verdict = "no_change"

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", self.message[:MESSAGE_CHARS])
        if self.added_text is not None:
            object.__setattr__(self, "added_text", self.added_text[:ADDED_TEXT_CHARS])


@dataclass(frozen=True)
class Segment:
    """A run of Turns whose messages are replayed verbatim (transcript memory).

    Model, Profile, prompt version and effort stay fixed inside a segment.
    `summary` is the `<earlier_turns>` text its first message opens with, or
    None when there were no earlier Turns.
    """

    segment_id: str
    model: str
    profile: ProfileName
    prompt_version: str
    effort: str | None
    opened_by: SegmentOpening
    summary: str | None


@dataclass(frozen=True)
class Resolution:
    """What one `resolve` call decided, so the next Turn knows (agent design §6.4 step 5).

    `at` is an RFC 3339 UTC time; the tuples hold Hunk ids. Stale Hunks were
    not applied and stay undecided.
    """

    proposal_id: str
    at: str
    applied: tuple[str, ...]
    rejected: tuple[str, ...]
    stale: tuple[str, ...]


@dataclass(frozen=True)
class ThreadMemory:
    """What the next Turn of a Thread starts from.

    `transcript` is the current segment's messages, exactly as sent and
    received (transcript memory only; empty otherwise). `since_last_turn`
    holds the resolutions recorded after the last Turn; `resolutions` holds
    every resolution of the Thread, in order.
    """

    thread_id: str
    records: tuple[TurnRecord, ...]
    segment: Segment | None
    transcript: tuple[Message, ...]
    since_last_turn: tuple[Resolution, ...]
    resolutions: tuple[Resolution, ...] = ()


@dataclass(frozen=True)
class ThreadInfo:
    """A Thread as `GET /api/projects/{id}/threads` lists it (agent design §10.3)."""

    thread_id: str
    title: str
    created_at: str
    updated_at: str
    turns: int
    last_summary: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
            "last_summary": self.last_summary,
        }


@dataclass(frozen=True)
class ThreadView:
    """A Thread with its Turns, oldest first, as `GET .../threads/{thread_id}` returns it.

    Each Turn has the §10.3 fields: `turn_id`, `at`, `message`, `reply`,
    `summary`, `status`, `outcome`, `verdict`, `proposal_id`, `model`, `profile`.
    """

    thread_id: str
    title: str
    turns: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "turns": [dict(turn) for turn in self.turns],
        }


# -- the store --------------------------------------------------------------------


@dataclass
class _Thread:
    """One Thread file, read and folded."""

    thread_id: str
    created_at: str
    title: str | None
    updated_at: str
    records: list[TurnRecord] = field(default_factory=list[TurnRecord])
    segment: Segment | None = None
    transcript: list[Message] = field(default_factory=list[Message])
    since: list[Resolution] = field(default_factory=list[Resolution])
    resolutions: list[Resolution] = field(default_factory=list[Resolution])

    def info(self) -> ThreadInfo:
        return ThreadInfo(
            thread_id=self.thread_id,
            title=_display_title(self.title, self.records),
            created_at=self.created_at,
            updated_at=self.updated_at,
            turns=len(self.records),
            last_summary=self.records[-1].summary if self.records else None,
        )

    def memory(self) -> ThreadMemory:
        return ThreadMemory(
            thread_id=self.thread_id,
            records=tuple(self.records),
            segment=self.segment,
            transcript=tuple(self.transcript),
            since_last_turn=tuple(self.since),
            resolutions=tuple(self.resolutions),
        )


class ThreadStore:
    """The Threads of every Project under one data directory.

    Thread ids are generated by `create`. Methods that take a Thread raise
    ThreadNotFound for an unknown id; titles are 1 to 120 characters after
    trimming, else ValueError. Writes are serialized within the process.
    """

    def __init__(self, data_dir: Path) -> None:
        self._layout = DataLayout(data_dir)
        self._lock = threading.Lock()

    def create(self, project_id: str, title: str | None = None) -> ThreadInfo:
        """Start a Thread; a Thread without a title shows its first request."""
        clean = None if title is None else _check_title(title)
        folder = self._folder(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        thread_id = uuid.uuid4().hex
        at = _now()
        line = _line("thread", {"thread_id": thread_id, "created_at": at, "title": clean})
        with self._lock, (folder / f"{thread_id}.jsonl").open("xb") as out:
            out.write(line)
            out.flush()
            os.fsync(out.fileno())
        return ThreadInfo(thread_id, clean or DEFAULT_TITLE, at, at, 0, None)

    def exists(self, project_id: str, thread_id: str) -> bool:
        return (
            _valid(project_id) and _valid(thread_id) and self._path(project_id, thread_id).is_file()
        )

    def load(self, project_id: str, thread_id: str) -> ThreadMemory:
        return self._read(project_id, thread_id).memory()

    def append_turn(
        self,
        project_id: str,
        thread_id: str,
        record: TurnRecord,
        messages: Sequence[Message],
        segment: Segment | None,
    ) -> None:
        """Append a Turn record, with the segment it opened and the messages it added.

        `segment` is None when the Turn continued the current segment, or under
        records memory, where `messages` is empty. Messages need a segment: the
        one given, or the Thread's current one. One write, then fsync.
        """
        path = self._path(project_id, thread_id)
        lines: list[bytes] = []
        if segment is not None:
            lines.append(_line("segment", {"turn_id": record.turn_id, **_segment_json(segment)}))
        if messages:
            if segment is None and self._read(project_id, thread_id).segment is None:
                raise ValueError("messages need a segment: this Turn opens none and none is open")
            lines.append(
                _line(
                    "messages",
                    {"turn_id": record.turn_id, "messages": [_message_json(m) for m in messages]},
                )
            )
        lines.append(_line("turn", _record_json(record)))
        self._append(path, thread_id, lines)

    def append_resolution(self, project_id: str, thread_id: str, resolution: Resolution) -> None:
        """Record what a `resolve` call decided; ThreadNotFound if the Thread was deleted."""
        line = _line(
            "resolution",
            {
                "proposal_id": resolution.proposal_id,
                "at": resolution.at,
                "applied": list(resolution.applied),
                "rejected": list(resolution.rejected),
                "stale": list(resolution.stale),
            },
        )
        self._append(self._path(project_id, thread_id), thread_id, [line])

    def rename(self, project_id: str, thread_id: str, title: str) -> ThreadInfo:
        clean = _check_title(title)
        path = self._path(project_id, thread_id)
        self._append(path, thread_id, [_line("title", {"at": _now(), "title": clean})])
        return self._read(project_id, thread_id).info()

    def delete(self, project_id: str, thread_id: str) -> None:
        path = self._path(project_id, thread_id)
        with self._lock:
            try:
                _retry(path.unlink)
            except FileNotFoundError:
                raise ThreadNotFound(thread_id) from None

    def list(self, project_id: str) -> list[ThreadInfo]:
        """Every readable Thread of the Project, most recently updated first."""
        if not _valid(project_id):
            return []
        folder = self._folder(project_id)
        if not folder.is_dir():
            return []
        infos: list[ThreadInfo] = []
        for path in folder.glob("*.jsonl"):
            if not _valid(path.stem):
                continue
            try:
                thread = _fold(path.stem, _items(path.read_bytes()))
            except OSError:
                continue
            if thread is not None:
                infos.append(thread.info())
        infos.sort(key=lambda info: (info.updated_at, info.thread_id), reverse=True)
        return infos

    def view(self, project_id: str, thread_id: str) -> ThreadView:
        thread = self._read(project_id, thread_id)
        turns = tuple(
            {
                "turn_id": r.turn_id,
                "at": r.at,
                "message": r.message,
                "reply": r.reply,
                "summary": r.summary,
                "status": r.status,
                "outcome": r.outcome,
                "verdict": r.verdict,
                "proposal_id": r.proposal_id,
                "model": r.model,
                "profile": r.profile,
            }
            for r in thread.records
        )
        return ThreadView(thread.thread_id, _display_title(thread.title, thread.records), turns)

    # -- internals ----------------------------------------------------------------

    def _folder(self, project_id: str) -> Path:
        if not _valid(project_id):
            raise ValueError("project ids must be single path segments")
        return self._layout.state(project_id) / "threads"

    def _path(self, project_id: str, thread_id: str) -> Path:
        if not _valid(thread_id):
            raise ThreadNotFound(thread_id)
        return self._folder(project_id) / f"{thread_id}.jsonl"

    def _read(self, project_id: str, thread_id: str) -> _Thread:
        path = self._path(project_id, thread_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ThreadNotFound(thread_id) from None
        thread = _fold(thread_id, _items(data))
        if thread is None:
            raise ThreadNotFound(thread_id)
        return thread

    def _append(self, path: Path, thread_id: str, lines: Sequence[bytes]) -> None:
        with self._lock:
            try:
                out = path.open("r+b")  # never creates a Thread file
            except FileNotFoundError:
                raise ThreadNotFound(thread_id) from None
            with out:
                end = out.seek(0, os.SEEK_END)
                lead = b""
                if end:
                    out.seek(end - 1)
                    if out.read(1) != b"\n":
                        lead = b"\n"  # a line cut off by a crash stays a line of its own
                    out.seek(0, os.SEEK_END)
                out.write(lead + b"".join(lines))
                out.flush()
                os.fsync(out.fileno())


# -- memory rendering (agent design §5.4) --------------------------------------------


def render_records(memory: ThreadMemory, *, profile: ProfileName, limit: int | None = None) -> str:
    """`<memory>`: the last 3 (guided) or 6 (agentic) Turn records, "" when there are none.

    T-1 is the most recent Turn. The agentic rendering adds the previous
    Turn's reply and the text it added. `limit` renders fewer records.
    """
    count = MEMORY_TURNS[profile] if limit is None else limit
    if count <= 0 or not memory.records:
        return ""
    lines = _record_lines(memory.records, memory.resolutions, count)
    last = memory.records[-1]
    if profile == "agentic":
        if last.added_text:
            lines.append(f'Text added in T-1: "{_one_line(last.added_text, _ADDED_CHARS)}"')
        if last.reply:
            lines.append(f'Your reply in T-1: "{_one_line(last.reply, _REPLY_CHARS)}"')
    return _element("memory", "\n".join(lines))


def earlier_turns(records: Sequence[TurnRecord], resolutions: Sequence[Resolution] = ()) -> str:
    """`<earlier_turns>`: a deterministic summary of the last 12 Turn records, "" for none.

    Each line gives the request, the outcome line, the files and lines changed
    and what the author decided; nothing depends on the clock.
    """
    if not records:
        return ""
    lines = ["Summary of the earlier turns of this conversation, oldest first."]
    lines.extend(_record_lines(records, resolutions, EARLIER_TURNS))
    return _element("earlier_turns", "\n".join(lines))


def render_since(memory: ThreadMemory, *, changed_files: Sequence[str] = ()) -> str:
    """`<since_last_turn>`: what changed since the previous Turn, "" when nothing did.

    The Hunks the author accepted and rejected, those that were stale, the
    files the author changed (given by the caller), and whether the previous
    Turn was stopped before it finished.
    """
    applied = sum(len(r.applied) for r in memory.since_last_turn)
    rejected = sum(len(r.rejected) for r in memory.since_last_turn)
    stale = sum(len(r.stale) for r in memory.since_last_turn)
    lines: list[str] = []
    decided = [
        f"accepted {_count(applied, 'change')}" if applied else "",
        f"rejected {_count(rejected, 'change')}" if rejected else "",
    ]
    if applied or rejected:
        lines.append(f"The author {' and '.join(d for d in decided if d)} of the proposal.")
    if stale:
        lines.append(
            f"{_count(stale, 'change')} could not be applied because the text had changed; "
            f"{'it is' if stale == 1 else 'they are'} still open."
        )
    if changed_files:
        lines.append(f"The author edited {', '.join(changed_files)}.")
    if memory.records and memory.records[-1].status in _STOPPED:
        lines.append("Your previous turn was stopped before it finished.")
    return _element("since_last_turn", "\n".join(lines)) if lines else ""


def _record_lines(
    records: Sequence[TurnRecord], resolutions: Sequence[Resolution], count: int
) -> list[str]:
    shown = list(records[-count:])
    offset = len(records) - len(shown)
    lines: list[str] = []
    for i, record in enumerate(shown):
        later = records[offset + i + 1 :]
        pieces = [
            f'T-{len(shown) - i}: "{_one_line(record.message, _LINE_MESSAGE_CHARS)}" ->',
            _one_line(record.summary, _LINE_SUMMARY_CHARS) or f"{record.outcome}.",
        ]
        where = _targets(record.targets)
        if where:
            pieces.append(f"Changed {where}.")
        state = _review(record, resolutions, later)
        if state:
            pieces.append(state)
        lines.append(" ".join(pieces))
    return lines


def _review(
    record: TurnRecord, resolutions: Sequence[Resolution], later: Sequence[TurnRecord]
) -> str:
    """What became of the Turn's Proposal."""
    if record.proposal_id is None:
        return ""
    mine = [r for r in resolutions if r.proposal_id == record.proposal_id]
    applied = sum(len(r.applied) for r in mine)
    rejected = sum(len(r.rejected) for r in mine)
    if applied or rejected:
        parts = [
            f"{applied} accepted" if applied else "",
            f"{rejected} rejected" if rejected else "",
        ]
        return f"Review: {', '.join(p for p in parts if p)}."
    if any(r.proposal_id not in (None, record.proposal_id) for r in later):
        return "Replaced by a later proposal."
    return "Pending review."


def _targets(targets: Sequence[RegionRef]) -> str:
    by_path: dict[str, list[str]] = {}
    for t in targets:
        if t.start_line > t.end_line:
            span = f"after L{t.end_line}"
        elif t.start_line == t.end_line:
            span = f"L{t.start_line}"
        else:
            span = f"L{t.start_line}-L{t.end_line}"
        by_path.setdefault(t.path, []).append(span)
    return "; ".join(f"{path} {', '.join(spans)}" for path, spans in by_path.items())


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _element(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def _display_title(title: str | None, records: Sequence[TurnRecord]) -> str:
    if title:
        return title
    for record in records:
        first = _one_line(record.message, _TITLE_FROM_MESSAGE_CHARS)
        if first:
            return first
    return DEFAULT_TITLE


# -- the file format ----------------------------------------------------------------


def _line(kind: str, body: Mapping[str, Any]) -> bytes:
    data = {"v": THREAD_VERSION, "type": kind, **body}
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    return text.encode("utf-8", "surrogatepass")


def _items(data: bytes) -> Iterator[Mapping[str, Any]]:
    """The v1 records of a Thread file; torn, unknown and foreign lines are skipped."""
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw.decode("utf-8", "surrogatepass"))
        except (UnicodeDecodeError, ValueError):
            continue  # a line cut off by a crash
        if isinstance(item, dict) and item.get("v") == THREAD_VERSION:  # pyright: ignore[reportUnknownMemberType]
            yield item  # pyright: ignore[reportUnknownArgumentType]


def _fold(thread_id: str, items: Iterable[Mapping[str, Any]]) -> _Thread | None:
    """Replay a Thread's records; None when it has no `thread` record."""
    thread: _Thread | None = None
    opened: tuple[str, Segment] | None = None
    added: tuple[str, list[Message]] | None = None
    for item in items:
        kind = item.get("type")
        try:
            if kind == "thread":
                if thread is None and item["thread_id"] == thread_id:
                    created = _str(item["created_at"])
                    thread = _Thread(thread_id, created, _opt_str(item["title"]), created)
                continue
            if thread is None:
                continue
            if kind == "title":
                thread.title = _str(item["title"])
                thread.updated_at = _str(item["at"])
            elif kind == "segment":
                opened = (_str(item["turn_id"]), _segment_from(item))
            elif kind == "messages":
                added = (_str(item["turn_id"]), [_message_from(m) for m in _list(item["messages"])])
            elif kind == "turn":
                record = _record_from(item)
                _commit(thread, record, opened, added)
                opened = added = None
            elif kind == "resolution":
                resolution = _resolution_from(item)
                thread.resolutions.append(resolution)
                thread.since.append(resolution)
                thread.updated_at = resolution.at
        except (KeyError, TypeError, ValueError):
            continue
    return thread


def _commit(
    thread: _Thread,
    record: TurnRecord,
    opened: tuple[str, Segment] | None,
    added: tuple[str, list[Message]] | None,
) -> None:
    if opened is not None and opened[0] == record.turn_id:
        thread.segment = opened[1]
        thread.transcript = []
    elif thread.segment is not None and (record.model, record.profile) != (
        thread.segment.model,
        thread.segment.profile,
    ):
        # A Turn outside the segment (another model or Profile) ends it: its
        # messages are not in the transcript, so replaying it would skip a Turn.
        thread.segment = None
        thread.transcript = []
    if added is not None and added[0] == record.turn_id and thread.segment is not None:
        thread.transcript.extend(added[1])
    thread.records.append(record)
    thread.since = []
    thread.updated_at = record.at


def _record_json(record: TurnRecord) -> dict[str, Any]:
    return {
        "turn_id": record.turn_id,
        "at": record.at,
        "status": record.status,
        "outcome": record.outcome,
        "verdict": record.verdict,
        "model": record.model,
        "profile": record.profile,
        "message": record.message,
        "summary": record.summary,
        "reply": record.reply,
        "proposal_id": record.proposal_id,
        "targets": [
            {"path": t.path, "start_line": t.start_line, "end_line": t.end_line}
            for t in record.targets
        ],
        "added_text": record.added_text,
    }


def _record_from(item: Mapping[str, Any]) -> TurnRecord:
    return TurnRecord(
        turn_id=_str(item["turn_id"]),
        at=_str(item["at"]),
        status=_choice(item["status"], _STATUSES),
        outcome=_choice(item["outcome"], _OUTCOMES),
        model=_str(item["model"]),
        profile=_choice(item["profile"], _PROFILES),
        message=_str(item["message"]),
        summary=_str(item["summary"]),
        proposal_id=_opt_str(item["proposal_id"]),
        targets=tuple(
            RegionRef(_str(t["path"]), _int(t["start_line"]), _int(t["end_line"]))
            for t in _list(item["targets"])
        ),
        added_text=_opt_str(item["added_text"]),
        reply=_str(item["reply"]),
        verdict=_choice(item["verdict"], _VERDICTS),
    )


def _segment_json(segment: Segment) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "model": segment.model,
        "profile": segment.profile,
        "prompt_version": segment.prompt_version,
        "effort": segment.effort,
        "opened_by": segment.opened_by,
        "summary": segment.summary,
    }


def _segment_from(item: Mapping[str, Any]) -> Segment:
    return Segment(
        segment_id=_str(item["segment_id"]),
        model=_str(item["model"]),
        profile=_choice(item["profile"], _PROFILES),
        prompt_version=_str(item["prompt_version"]),
        effort=_opt_str(item["effort"]),
        opened_by=_choice(item["opened_by"], _OPENINGS),
        summary=_opt_str(item["summary"]),
    )


def _resolution_from(item: Mapping[str, Any]) -> Resolution:
    return Resolution(
        proposal_id=_str(item["proposal_id"]),
        at=_str(item["at"]),
        applied=_strs(item["applied"]),
        rejected=_strs(item["rejected"]),
        stale=_strs(item["stale"]),
    )


def _message_json(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "blocks": [_block_json(b) for b in message.blocks],
        "native": _plain(message.native),
    }


def _message_from(item: Any) -> Message:
    if not isinstance(item, dict):
        raise ValueError("a message is an object")
    data: Mapping[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
    role = _choice(data["role"], _ROLES)
    native = data["native"]
    if native is not None and not isinstance(native, dict):
        raise ValueError("native is an object or null")
    blocks = tuple(_block_from(b) for b in _list(data["blocks"]))
    return Message(role, blocks, native)  # pyright: ignore[reportUnknownArgumentType]


def _block_json(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": _plain(block.input),
        }
    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
    }


def _block_from(item: Any) -> Block:
    if not isinstance(item, dict):
        raise ValueError("a block is an object")
    data: Mapping[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
    kind = data["type"]
    if kind == "text":
        return TextBlock(_str(data["text"]))
    if kind == "tool_use":
        tool_input = data["input"]
        if not isinstance(tool_input, dict):
            raise ValueError("tool input is an object")
        return ToolUseBlock(_str(data["id"]), _str(data["name"]), tool_input)  # pyright: ignore[reportUnknownArgumentType]
    if kind == "tool_result":
        is_error = data["is_error"]
        if not isinstance(is_error, bool):
            raise ValueError("is_error is a boolean")
        return ToolResultBlock(_str(data["tool_use_id"]), _str(data["content"]), is_error)
    raise ValueError(f"unknown block type {kind!r}")


def _plain(value: Any) -> Any:
    """`value` with every Mapping a dict and every tuple a list, ready for JSON."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


# -- helpers -----------------------------------------------------------------------


def _valid(value: str) -> bool:
    return _ID.fullmatch(value) is not None


def _check_title(title: str) -> str:
    clean = " ".join(title.split())
    if not 1 <= len(clean) <= TITLE_CHARS:
        raise ValueError(f"a thread title has 1 to {TITLE_CHARS} characters")
    return clean


def _now() -> str:
    """RFC 3339 UTC with milliseconds, as in "2026-11-24T23:10:04.120Z"."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _retry(action: Callable[[], None]) -> None:
    for attempt in range(_RETRIES):
        try:
            action()
            return
        except PermissionError:
            if attempt + 1 == _RETRIES:
                raise
            time.sleep(_RETRY_DELAY_S)


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return value


def _opt_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")
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
