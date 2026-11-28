"""The Trace of a Turn: every event in order, on disk and in memory (agent design §2.2).

Two files per Turn, under the data directory:

- `state/<project_id>/turns/<turn_id>.jsonl`, the Trace: one envelope per line,
  public and internal, appended and flushed before subscribers are notified.
- `state/<project_id>/turns/<turn_id>.json`, the manifest: the Turn's state and,
  once it is finished, its outcome. It is always replaced atomically.

`seq` counts public events from 1, with no gaps. An internal envelope takes no
number of its own: it carries the `seq` of the public event before it (0 before
the first). `done` is written exactly once, last, with fsync; nothing follows it.

While the Turn is live, subscribers read the writer's in-memory list, so none
can lag or be dropped. After `release()`, and for every reader `open_trace`
returns, they replay from the file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Final

from prismtex.agent.errors import TurnNotFound
from prismtex.agent.events import INTERNAL_TYPES, Envelope, Event
from prismtex.agent.types import (
    ErrorInfo,
    Outcome,
    ProfileName,
    TurnOutcome,
    TurnState,
    TurnStatus,
    Usage,
    Verdict,
)
from prismtex.layout import DataLayout

MANIFEST_VERSION: Final = 1
DEFAULT_KEEP: Final = 200

# A reader can hold the manifest open on Windows without delete sharing, so
# os.replace raises PermissionError; it is retried this many times, 50 ms apart.
_REPLACE_RETRIES: Final = 5
_REPLACE_DELAY_S: Final = 0.05
_TEMP_PREFIX: Final = ".prismtex-"
_TEMP_SUFFIX: Final = ".tmp"

# Project and Turn ids name files, so they are single safe path segments.
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class Manifest:
    """A Turn's state and, once finished, how it ended (agent design §2.2)."""

    turn_id: str
    thread_id: str
    project_id: str
    state: TurnState
    model: str
    profile: ProfileName
    prompt_version: str
    started_at: str
    finished_at: str | None
    status: TurnStatus | None
    outcome: Outcome | None
    verdict: Verdict | None
    summary: str | None
    proposal_id: str | None
    usage: Usage
    error: ErrorInfo | None


@dataclass(frozen=True)
class _Files:
    jsonl: Path
    manifest: Path


class TraceWriter:
    """Writes one Turn's Trace and manifest, and fans events out to live readers.

    A writer over a Trace that already exists continues it: a line cut off by
    a crash is dropped and `seq` goes on from the last public event, so the
    engine can close an interrupted Turn. Over a finished Trace, `emit` raises.
    `emit` may be called from any thread.
    """

    def __init__(
        self, data_dir: Path, project_id: str, turn_id: str, *, manifest: Manifest
    ) -> None:
        if not (_valid_id(project_id) and _valid_id(turn_id)):
            raise ValueError("project and turn ids must be single path segments")
        if (manifest.project_id, manifest.turn_id) != (project_id, turn_id):
            raise ValueError("the manifest belongs to another Turn")
        self.project_id = project_id
        self.turn_id = turn_id
        self._files = _files(data_dir, project_id, turn_id)
        self._lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._waiters: list[asyncio.Future[None]] = []
        self._manifest = manifest
        self._files.jsonl.parent.mkdir(parents=True, exist_ok=True)
        existing = _resume(self._files.jsonl)
        public = [env for env in existing if not env.internal]
        self._envelopes: list[Envelope] | None = existing
        self._seq = public[-1].seq if public else 0
        done = [env for env in public if env.type == "done"]
        self._done_at: str | None = done[0].at if done else None
        _write_manifest(self._files.manifest, manifest)
        self._file: BinaryIO | None = None if done else self._files.jsonl.open("ab")

    def emit(self, event: Event, *, internal: bool = False) -> Envelope:
        """Number, append and flush the event, then notify subscribers."""
        hidden = internal or event.type in INTERNAL_TYPES
        if hidden and event.type == "done":
            raise ValueError("done is always public")
        with self._lock:
            if self._file is None:
                raise RuntimeError(f"Turn {self.turn_id} already emitted done")
            seq = self._seq if hidden else self._seq + 1
            envelope = Envelope.of(event, seq=seq, turn_id=self.turn_id, at=_now(), internal=hidden)
            self._file.write(envelope.to_json().encode("utf-8") + b"\n")
            self._file.flush()
            self._seq = seq
            if envelope.type == "done" and not hidden:
                os.fsync(self._file.fileno())
                self._file.close()
                self._file = None
                self._done_at = envelope.at
            if self._envelopes is not None:
                self._envelopes.append(envelope)
            waiters, self._waiters = self._waiters, []
        _wake(waiters)
        return envelope

    def set_state(self, state: TurnState) -> None:
        """Record a state transition in the manifest."""
        with self._manifest_lock:
            self._manifest = replace(self._manifest, state=state)
            _write_manifest(self._files.manifest, self._manifest)

    def close(self, outcome: TurnOutcome) -> None:
        """Write the outcome into the manifest, durably; call it after `done`."""
        if outcome.turn_id != self.turn_id:
            raise ValueError("the outcome belongs to another Turn")
        with self._lock:
            finished_at = self._done_at
        if finished_at is None:
            raise RuntimeError("close() comes after done")
        with self._manifest_lock:
            self._manifest = replace(
                self._manifest,
                state="done",
                finished_at=finished_at,
                status=outcome.status,
                outcome=outcome.outcome,
                verdict=outcome.verdict,
                summary=outcome.summary,
                proposal_id=outcome.proposal.id if outcome.proposal is not None else None,
                usage=outcome.usage,
                error=outcome.error,
            )
            _write_manifest(self._files.manifest, self._manifest, durable=True)

    def reader(self) -> TraceReader:
        """A reader that follows this writer live."""
        return TraceReader(self._files, writer=self)

    def release(self) -> None:
        """Drop the in-memory list; readers replay from the file from now on."""
        with self._lock:
            if self._file is not None:
                raise RuntimeError("release() comes after done")
            self._envelopes = None

    # -- for TraceReader ----------------------------------------------------------

    def _current_manifest(self) -> Manifest:
        with self._manifest_lock:
            return self._manifest

    def _snapshot(self) -> list[Envelope] | None:
        with self._lock:
            return None if self._envelopes is None else list(self._envelopes)

    def _take(self, index: int) -> tuple[list[Envelope] | None, asyncio.Future[None] | None]:
        """Envelopes from `index` on, or a future to await when there are none yet.

        None instead of a list means: read the file (the list was released,
        or the writer has nothing more to give).
        """
        with self._lock:
            if self._envelopes is None:
                return None, None
            batch = self._envelopes[index:]
            if batch:
                return batch, None
            if self._file is None:
                return None, None
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            return batch, waiter


class TraceReader:
    """Reads one Turn's Trace: live from its writer, or from the files."""

    def __init__(self, files: _Files, *, writer: TraceWriter | None = None) -> None:
        self._files = files
        self._writer = writer

    async def subscribe(self, after: int = 0) -> AsyncIterator[Envelope]:
        """Every public envelope with `seq > after`, in order; ends after `done`.

        A reader over the files of a Trace that has no `done` ends at the end
        of the file.
        """
        last = after
        writer = self._writer
        if writer is not None:
            index = 0
            while True:
                batch, waiter = writer._take(index)
                if batch is None:
                    break
                for env in batch:
                    index += 1
                    if env.internal:
                        continue
                    if env.seq > last:
                        last = env.seq
                        yield env
                    if env.type == "done":
                        return
                if waiter is not None:
                    await waiter
        for env in _read_envelopes(self._files.jsonl):
            if env.internal:
                continue
            if env.seq > last:
                last = env.seq
                yield env
            if env.type == "done":
                return

    def manifest(self) -> Manifest:
        if self._writer is not None:
            return self._writer._current_manifest()
        return _read_manifest(self._files.manifest)

    def envelopes(self, *, internal: bool = False) -> list[Envelope]:
        """The envelopes written so far; internal ones only when asked for."""
        items = self._writer._snapshot() if self._writer is not None else None
        if items is None:
            items = _read_envelopes(self._files.jsonl)
        return [env for env in items if internal or not env.internal]


def open_trace(data_dir: Path, project_id: str, turn_id: str) -> TraceReader:
    """A reader over a Turn's files; raises TurnNotFound when it has no manifest."""
    if not (_valid_id(project_id) and _valid_id(turn_id)):
        raise TurnNotFound(turn_id)
    files = _files(data_dir, project_id, turn_id)
    if not files.manifest.is_file():
        raise TurnNotFound(turn_id)
    return TraceReader(files)


def prune_traces(data_dir: Path, project_id: str, *, keep: int = DEFAULT_KEEP) -> int:
    """Delete finished Traces beyond the newest `keep`, by start time; returns how many.

    A Trace whose Turn has not finished is never deleted. A file another
    process holds open is left for the next prune.
    """
    if keep < 0:
        raise ValueError("keep must be at least 0")
    if not _valid_id(project_id):
        return 0
    folder = DataLayout(data_dir).state(project_id) / "turns"
    if not folder.is_dir():
        return 0
    traces: list[tuple[datetime, str, bool]] = []
    for path in folder.glob("*.json"):
        if not _valid_id(path.stem):
            continue
        try:
            manifest = _read_manifest(path)
        except (OSError, ValueError):
            continue  # unreadable: never delete what cannot be identified
        traces.append((_parse_time(manifest.started_at), path.stem, manifest.state == "done"))
    traces.sort(reverse=True)
    removed = 0
    for _, turn_id, finished in traces[keep:]:
        if not finished:
            continue
        try:
            # The Trace first: a manifest left behind is still a valid, empty Turn.
            (folder / f"{turn_id}.jsonl").unlink(missing_ok=True)
            (folder / f"{turn_id}.json").unlink()
        except OSError:
            continue
        removed += 1
    return removed


# -- helpers ---------------------------------------------------------------------


def _valid_id(value: str) -> bool:
    return _ID.fullmatch(value) is not None


def _files(data_dir: Path, project_id: str, turn_id: str) -> _Files:
    folder = DataLayout(data_dir).state(project_id) / "turns"
    return _Files(jsonl=folder / f"{turn_id}.jsonl", manifest=folder / f"{turn_id}.json")


def _now() -> str:
    """RFC 3339 UTC with milliseconds, as in "2026-11-24T23:10:04.120Z"."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _wake(waiters: list[asyncio.Future[None]]) -> None:
    for waiter in waiters:
        try:
            waiter.get_loop().call_soon_threadsafe(_resolve, waiter)
        except RuntimeError:
            pass  # its event loop is closed; nobody is waiting any more


def _resolve(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


def _parse_lines(data: bytes) -> list[Envelope]:
    envelopes: list[Envelope] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            envelopes.append(Envelope.from_json(line))
        except ValueError:
            continue  # a line cut off by a crash
    return envelopes


def _read_envelopes(path: Path) -> list[Envelope]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return _parse_lines(data)


def _resume(path: Path) -> list[Envelope]:
    """The envelopes of an existing Trace, after cutting off an unfinished last line."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    if data and not data.endswith(b"\n"):
        complete = data.rfind(b"\n") + 1
        with path.open("r+b") as out:
            out.truncate(complete)
        data = data[:complete]
    return _parse_lines(data)


def _manifest_json(manifest: Manifest) -> dict[str, Any]:
    return {"v": MANIFEST_VERSION, **asdict(manifest)}


def _read_manifest(path: Path) -> Manifest:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or data.get("v") != MANIFEST_VERSION:
        raise ValueError(f"{path.name} is not a v{MANIFEST_VERSION} Turn manifest")
    body: dict[str, Any] = dict(data)
    try:
        values = {f.name: body[f.name] for f in fields(Manifest)}
        values["usage"] = Usage(**_known(Usage, values["usage"]))
        if values["error"] is not None:
            values["error"] = ErrorInfo(**_known(ErrorInfo, values["error"]))
        return Manifest(**values)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"{path.name} is not a complete Turn manifest") from exc


def _known(cls: type[Usage] | type[ErrorInfo], data: Mapping[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _write_manifest(path: Path, manifest: Manifest, *, durable: bool = False) -> None:
    data = json.dumps(_manifest_json(manifest), ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data.encode("utf-8"))
            if durable:
                out.flush()
                os.fsync(out.fileno())
        _replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _replace(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_DELAY_S)
