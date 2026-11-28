"""Run one external command with no shell, closed stdin, a timeout and an output cap.

On timeout the whole process tree is killed, not only the direct child:
latexmk starts the engine and BibTeX as children of its own.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

__all__ = ["ExecutableNotFound", "RunOutcome", "run_hardened"]

_CHUNK = 64 * 1024
# How long to wait for the output pipe to close once the command has exited
# or been killed. A leftover process that still holds it open is killed.
_DRAIN_GRACE_S = 2.0


class ExecutableNotFound(Exception):
    """argv[0] is not on the PATH given in the command's environment."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} was not found on PATH")
        self.name = name


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int | None  # None when the command was killed on timeout
    output: str  # stdout and stderr interleaved, decoded as UTF-8 with replacement
    timed_out: bool
    duration_ms: int
    truncated: bool  # the output passed max_output_bytes and the rest was dropped


class _CappedReader:
    """Keeps the first `limit` bytes of a pipe and drains the rest.

    Draining matters: a child blocked on a full pipe would never exit.
    """

    def __init__(self, pipe: IO[bytes], limit: int) -> None:
        # The reader holds the pipe and is the only thing that closes it, once
        # its read has ended. On Windows, closing a pipe while another thread
        # is blocked reading it waits for that read, so a leftover process that
        # holds the pipe would stall whoever dropped the last reference.
        self._pipe = pipe
        self._fd = pipe.fileno()
        self._limit = limit
        self._chunks: list[bytes] = []
        self._kept = 0
        self.truncated = False
        self._thread = threading.Thread(target=self._run, name="prismtex-output", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while chunk := os.read(self._fd, _CHUNK):
                room = self._limit - self._kept
                if len(chunk) > room:
                    chunk = chunk[: max(room, 0)]
                    self.truncated = True
                if chunk:
                    self._chunks.append(chunk)
                    self._kept += len(chunk)
        except OSError:
            pass
        finally:
            self._pipe.close()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        subprocess.run(
            [str(taskkill), "/T", "/F", "/PID", str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            proc.kill()
        except OSError:
            pass
    else:
        # start_new_session made the child the leader of a new process group
        # whose id is its pid; the engine and BibTeX are in that group too.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_hardened(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    max_output_bytes: int = 4 * 2**20,
) -> RunOutcome:
    """Run `argv` to completion or until `timeout_s` passes.

    The executable is looked up on `env["PATH"]`, not the server's PATH, and
    started by absolute path. Raises ExecutableNotFound if it is missing.
    """
    if not argv:
        raise ValueError("argv is empty")
    search_path = env.get("PATH")
    executable = shutil.which(argv[0], path=search_path) if search_path else None
    if executable is None:
        raise ExecutableNotFound(argv[0])

    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        new_session = False
    else:
        creationflags = 0
        new_session = True

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [executable, *argv[1:]],
            cwd=cwd,
            env=env,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=new_session,
        )
    except FileNotFoundError as exc:
        raise ExecutableNotFound(argv[0]) from exc
    assert proc.stdout is not None
    reader = _CappedReader(proc.stdout, max_output_bytes)

    exit_code: int | None = None
    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        proc.wait()
    except BaseException:
        _kill_tree(proc)
        proc.wait()
        raise
    finally:
        if not reader.join(_DRAIN_GRACE_S):
            _kill_tree(proc)
            # Still blocked means a process outside the tree holds the pipe
            # (on Windows, taskkill /T cannot reach the children of a parent
            # that has exited). The reader keeps draining and closes the pipe
            # when that process ends; the pipe is never closed under it here.
            reader.join(_DRAIN_GRACE_S)

    return RunOutcome(
        exit_code=exit_code,
        output=reader.text(),
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - started) * 1000),
        truncated=reader.truncated,
    )
