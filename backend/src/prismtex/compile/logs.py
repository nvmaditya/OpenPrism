"""Compile logs under `state/<id>/logs/`: `<log_id>.log` text and `<log_id>.json` metadata.

Only the newest KEEP_LOGS are kept. The JSON file is written last, so a log
without one is an interrupted save and is never listed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from prismtex.errors import LogNotFound
from prismtex.layout import DataLayout

__all__ = ["KEEP_LOGS", "CompileLogStore", "LogSummary"]

KEEP_LOGS = 20

_LOG_ID = re.compile(r"[0-9a-f]{32}")

LogSummary = dict[str, Any]


class CompileLogStore:
    def __init__(self, layout: DataLayout, *, keep: int = KEEP_LOGS) -> None:
        self.layout = layout
        self.keep = keep
        self._clock = _Clock()

    def save(
        self,
        project_id: str,
        *,
        status: str,
        engine: str,
        root_file: str,
        duration_ms: int,
        errors: int,
        warnings: int,
        text: str,
    ) -> str:
        """Store one compile log and prune old ones; returns the new log_id."""
        folder = self.layout.logs(project_id)
        folder.mkdir(parents=True, exist_ok=True)
        log_id = uuid.uuid4().hex
        meta: LogSummary = {
            "log_id": log_id,
            "created_at": self._clock.now(),
            "status": status,
            "engine": engine,
            "root_file": root_file,
            "duration_ms": duration_ms,
            "errors": errors,
            "warnings": warnings,
        }
        _atomic_write(folder / f"{log_id}.log", text.encode("utf-8", errors="replace"))
        _atomic_write(folder / f"{log_id}.json", json.dumps(meta, indent=2).encode("utf-8"))
        self._prune(folder)
        return log_id

    def list(self, project_id: str) -> list[LogSummary]:
        """Metadata of the kept logs, newest first."""
        return self._metas(self.layout.logs(project_id))

    def get(self, project_id: str, log_id: str) -> LogSummary:
        """Metadata plus `"log"`, the text; LogNotFound if there is no such log."""
        if not _LOG_ID.fullmatch(log_id):
            raise LogNotFound(log_id)
        folder = self.layout.logs(project_id)
        meta = _load_meta(folder / f"{log_id}.json")
        if meta is None or meta.get("log_id") != log_id:
            raise LogNotFound(log_id)
        try:
            data = (folder / f"{log_id}.log").read_bytes()
        except OSError as exc:
            raise LogNotFound(log_id) from exc
        return {**meta, "log": data.decode("utf-8", errors="replace")}

    def _metas(self, folder: Path) -> list[LogSummary]:
        if not folder.is_dir():
            return []
        metas = [
            meta
            for path in folder.glob("*.json")
            if _LOG_ID.fullmatch(path.stem) and (meta := _load_meta(path)) is not None
        ]
        metas.sort(key=lambda meta: (str(meta.get("created_at", "")), meta["log_id"]), reverse=True)
        return metas

    def _prune(self, folder: Path) -> None:
        kept = {meta["log_id"] for meta in self._metas(folder)[: self.keep]}
        for path in folder.iterdir():
            if path.suffix in (".json", ".log") and path.stem not in kept:
                path.unlink(missing_ok=True)


def _load_meta(path: Path) -> LogSummary | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("log_id") != path.stem:
        return None
    return data


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".prismtex-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class _Clock:
    """UTC ISO 8601 timestamps that strictly increase, so logs sort in save order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = datetime.min.replace(tzinfo=UTC)

    def now(self) -> str:
        with self._lock:
            now = datetime.now(UTC)
            if now <= self._last:
                now = self._last + timedelta(microseconds=1)
            self._last = now
        return now.isoformat(timespec="microseconds")
