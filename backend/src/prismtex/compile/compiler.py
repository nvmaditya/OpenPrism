"""Compiler: run latexmk over a project and report structured results.

latexmk runs with the project folder as its working directory and writes
everything to an output folder outside it, so the project folder never
gains build artifacts (ADR 0004).
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, get_args

from prismtex.compile.argv import build_latexmk_argv, build_tex_env, is_shell_safe_name
from prismtex.compile.logparse import (
    Diagnostic,
    combine_diagnostics,
    parse_blg,
    parse_log,
    parse_tool_output,
)
from prismtex.compile.logs import CompileLogStore, LogSummary
from prismtex.compile.runner import ExecutableNotFound, RunOutcome, run_hardened
from prismtex.errors import (
    InvalidRequest,
    PathPolicyError,
    PdfNotFound,
    ProjectNotFound,
    RootFileInvalid,
    RootFileMissing,
)
from prismtex.workspace import Engine, ProjectStore, check_relative_path, check_root_file

__all__ = [
    "MAX_LOG_TEXT_BYTES",
    "TEX_MISSING_MESSAGE",
    "CompileResult",
    "CompileService",
    "CompileStatus",
    "Compiler",
    "pdf_file",
]

CompileStatus = Literal["ok", "failed", "timeout", "unavailable"]

MAX_LOG_TEXT_BYTES = 2 * 2**20
TEX_MISSING_MESSAGE = (
    "latexmk was not found. Install TeX Live or MiKTeX (with Perl) "
    "and make sure latexmk is on PATH."
)

# latexmk prints this when the outputs match the sources, whether or not it
# had to run anything. After an unchanged re-run it is the only sign that
# the log on disk belongs to the current sources.
_UP_TO_DATE = re.compile(r"^Latexmk: All targets .* are up-to-date", re.MULTILINE)
_PREVIOUS_ERROR = "gave an error in previous invocation of latexmk"


@dataclass(frozen=True)
class CompileResult:
    status: CompileStatus
    diagnostics: tuple[Diagnostic, ...]
    pdf_path: Path | None  # absolute, inside out_dir; None unless this run produced it
    log_text: str  # latexmk output plus the TeX log, capped at MAX_LOG_TEXT_BYTES
    duration_ms: int
    engine: Engine
    root_file: str
    exit_code: int | None
    log_id: str | None = None  # set by Compiler.compile once the log is stored

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class CompileService(Protocol):
    """What the HTTP layer needs; tests inject a fake with these methods."""

    def compile(
        self, project_id: str, *, root_file: str | None = None, engine: Engine | None = None
    ) -> CompileResult: ...

    def logs(self, project_id: str) -> list[LogSummary]: ...

    def get_log(self, project_id: str, log_id: str) -> LogSummary: ...


class Compiler:
    def __init__(
        self,
        store: ProjectStore,
        *,
        timeout_s: float = 120.0,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        """`base_env` is the environment latexmk's is filtered from (default: os.environ)."""
        self.store = store
        self.timeout_s = timeout_s
        self._base_env = base_env
        self._logs = CompileLogStore(store.layout)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- project compiles ---------------------------------------------------

    def compile(
        self, project_id: str, *, root_file: str | None = None, engine: Engine | None = None
    ) -> CompileResult:
        """Compile a project's root file into `build/<id>/out` and store the log.

        `root_file` and `engine` override the project settings for this run
        only. One compile per project runs at a time; a second caller waits.
        """
        settings = self.store.settings(project_id)
        src_dir = self.store.project_dir(project_id)
        chosen = root_file if root_file is not None else settings.root_file
        if chosen is None:
            raise RootFileMissing()
        chosen = _check_root(src_dir, chosen)
        chosen_engine = _check_engine(engine if engine is not None else settings.engine)
        with self._lock(project_id):
            result = self.compile_dir(
                src_dir,
                project_id=project_id,
                root_file=chosen,
                engine=chosen_engine,
                out_dir=self.store.layout.out(project_id),
            )
            try:
                # The project may have been deleted while TeX ran; its
                # state folder must not come back.
                self.store.get(project_id)
            except ProjectNotFound:
                return result
            log_id = self._logs.save(
                project_id,
                status=result.status,
                engine=result.engine,
                root_file=result.root_file,
                duration_ms=result.duration_ms,
                errors=sum(d.severity == "error" for d in result.diagnostics),
                warnings=sum(d.severity == "warning" for d in result.diagnostics),
                text=result.log_text,
            )
        return replace(result, log_id=log_id)

    def logs(self, project_id: str) -> list[LogSummary]:
        """Stored compile logs, newest first, metadata only."""
        self.store.get(project_id)
        return self._logs.list(project_id)

    def get_log(self, project_id: str, log_id: str) -> LogSummary:
        """One stored log: its metadata plus `"log"`."""
        self.store.get(project_id)
        return self._logs.get(project_id, log_id)

    # -- any source folder ----------------------------------------------------

    def compile_dir(
        self,
        src_dir: Path,
        *,
        project_id: str,
        root_file: str,
        engine: Engine,
        out_dir: Path,
        log_path: Path | None = None,
        timeout_s: float | None = None,
    ) -> CompileResult:
        """Compile `root_file` in `src_dir` into `out_dir`; takes no lock.

        `project_id` names the project the folder belongs to (Phase 1 scratch
        compiles pass it); it is not looked up. `log_path`, when given, also
        receives the log text.
        """
        src = Path(src_dir).resolve()
        root = _check_root(src, root_file)
        engine = _check_engine(engine)
        out = Path(out_dir).resolve()
        if out == src or out.is_relative_to(src):
            raise ValueError("out_dir must be outside the source folder")
        out.mkdir(parents=True, exist_ok=True)
        limit = self.timeout_s if timeout_s is None else timeout_s

        stem = PurePosixPath(root).stem
        outputs = _Outputs.capture(out, stem)
        argv = build_latexmk_argv(root, engine, out)
        env = build_tex_env(os.environ if self._base_env is None else self._base_env, out)
        try:
            run = run_hardened(argv, cwd=src, env=env, timeout_s=limit)
            reading = _read_outputs(run, outputs, src)
            remaining = limit - run.duration_ms / 1000
            if _stale_rule_failure(run, outputs, reading) and remaining > 0:
                # latexmk 4.83 reports a rule's old failure once more after the
                # rule stops being used (the bibliography was removed, say).
                # The next run has nothing left to do and settles it.
                run = _joined(run, run_hardened(argv, cwd=src, env=env, timeout_s=remaining))
                reading = _read_outputs(run, outputs, src)
        except ExecutableNotFound:
            return self._finish_unavailable(TEX_MISSING_MESSAGE, root, engine, log_path)
        except OSError as exc:
            message = f"latexmk could not be started: {exc.strerror or exc}."
            return self._finish_unavailable(message, root, engine, log_path)

        if run.timed_out:
            if outputs.changed(outputs.pdf):
                # TeX was killed while writing it; a truncated PDF is useless.
                outputs.pdf.unlink(missing_ok=True)
            status: CompileStatus = "timeout"
            message = f"The compile was stopped after {limit:g} seconds."
            diagnostics = (Diagnostic(None, None, "error", "timeout", message),)
            pdf_path = None
        else:
            found = reading.found
            has_error = any(d.severity == "error" for d in found)
            pdf_path = outputs.pdf if reading.pdf_current else None
            ok = run.exit_code == 0 and pdf_path is not None and not has_error
            status = "ok" if ok else "failed"
            if not ok and not has_error:
                found = [*found, _failure_reason(run.exit_code, pdf_path, reading.log_text)]
            diagnostics = tuple(found)

        text = _cap_text(_combined_log(run.output, run.truncated, stem, reading.log_text))
        if log_path is not None:
            _write_text(log_path, text)
        return CompileResult(
            status=status,
            diagnostics=diagnostics,
            pdf_path=pdf_path,
            log_text=text,
            duration_ms=run.duration_ms,
            engine=engine,
            root_file=root,
            exit_code=run.exit_code,
        )

    # -- internals ----------------------------------------------------------

    def _lock(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.Lock())

    @staticmethod
    def _finish_unavailable(
        message: str, root: str, engine: Engine, log_path: Path | None
    ) -> CompileResult:
        if log_path is not None:
            _write_text(log_path, message + "\n")
        return CompileResult(
            status="unavailable",
            diagnostics=(Diagnostic(None, None, "error", "tex_missing", message),),
            pdf_path=None,
            log_text=message + "\n",
            duration_ms=0,
            engine=engine,
            root_file=root,
            exit_code=None,
        )


class _Outputs:
    """The PDF, TeX log and BibTeX log of one job, and their state before the run."""

    def __init__(self, pdf: Path, log: Path, blg: Path) -> None:
        self.pdf, self.log, self.blg = pdf, log, blg
        self._before = {path: _stamp(path) for path in (pdf, log, blg)}

    @classmethod
    def capture(cls, out_dir: Path, stem: str) -> _Outputs:
        return cls(*(out_dir / f"{stem}{suffix}" for suffix in (".pdf", ".log", ".blg")))

    def changed(self, path: Path) -> bool:
        """True when the run wrote `path`."""
        stamp = _stamp(path)
        return stamp is not None and stamp != self._before[path]


@dataclass(frozen=True)
class _Reading:
    log_text: str | None  # the TeX log, when it belongs to the current sources
    found: list[Diagnostic]
    pdf_current: bool


def _read_outputs(run: RunOutcome, outputs: _Outputs, src: Path) -> _Reading:
    """What a finished run left behind: the current log, its diagnostics, the PDF."""
    up_to_date = _UP_TO_DATE.search(run.output) is not None
    current_log = outputs.changed(outputs.log) or up_to_date
    log_text = _read_text(outputs.log) if current_log else None
    groups: list[list[Diagnostic]] = []
    if log_text is not None:
        groups.append(parse_log(log_text, src_dir=src))
    # A .blg BibTeX did not rewrite still describes the bibliography if the
    # document still reads the .bbl; otherwise it is left over.
    bbl = outputs.pdf.with_suffix(".bbl").name
    if outputs.blg.is_file() and (outputs.changed(outputs.blg) or _mentions(log_text, bbl)):
        groups.append(parse_blg(_read_text(outputs.blg) or "", src_dir=src))
    groups.append(parse_tool_output(run.output))
    # After a failed run whose sources have not changed, latexmk exits non-zero
    # and still says "up-to-date"; the PDF on disk then predates that failure.
    pdf_current = outputs.changed(outputs.pdf) or (
        run.exit_code == 0 and up_to_date and outputs.pdf.is_file()
    )
    return _Reading(log_text, combine_diagnostics(*groups), pdf_current)


def _stale_rule_failure(run: RunOutcome, outputs: _Outputs, reading: _Reading) -> bool:
    """latexmk failed only because of a rule that errored in an earlier run."""
    return (
        run.exit_code not in (None, 0)
        and _PREVIOUS_ERROR in run.output
        and outputs.changed(outputs.pdf)
        and reading.log_text is not None
        and not any(d.severity == "error" for d in reading.found)
    )


def _joined(first: RunOutcome, second: RunOutcome) -> RunOutcome:
    return RunOutcome(
        exit_code=second.exit_code,
        output=first.output + second.output,
        timed_out=second.timed_out,
        duration_ms=first.duration_ms + second.duration_ms,
        truncated=first.truncated or second.truncated,
    )


def pdf_file(store: ProjectStore, project_id: str, root_file: str | None = None) -> Path:
    """The compiled PDF of the project's root file, or of `root_file`; PdfNotFound if none."""
    name = root_file if root_file is not None else store.settings(project_id).root_file
    if name is None:
        raise PdfNotFound()
    try:
        rel = check_relative_path(name)
    except PathPolicyError as exc:
        raise PdfNotFound() from exc
    if not rel.lower().endswith(".tex"):
        raise PdfNotFound()
    # TeX names its output after the root file's stem, whatever folder it is in.
    path = store.layout.out(project_id) / f"{PurePosixPath(rel).stem}.pdf"
    if not path.is_file():
        raise PdfNotFound()
    return path


def _check_root(src_dir: Path, root_file: str) -> str:
    root = check_root_file(src_dir, root_file)
    if not is_shell_safe_name(root):
        raise RootFileInvalid("bad_char", 'The root file name may not contain any of ` $ \\ " % !.')
    return root


def _check_engine(engine: str) -> Engine:
    for known in get_args(Engine):
        if engine == known:
            return known
    raise InvalidRequest("The engine must be pdflatex, xelatex or lualatex.", field="engine")


_Stamp = tuple[int, int, int]


def _stamp(path: Path) -> _Stamp | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def _mentions(text: str | None, name: str) -> bool:
    if text is None:
        return False
    return re.search(rf"(?:^|[\s(/\\]){re.escape(name)}\b", text, re.MULTILINE) is not None


def _failure_reason(
    exit_code: int | None, pdf_path: Path | None, log_text: str | None
) -> Diagnostic:
    """Why a compile failed when the logs name no error."""
    if log_text is None:
        message = f"latexmk exited with code {exit_code} before TeX wrote a log."
    elif pdf_path is None:
        message = "No PDF was produced."
        if "No pages of output." in log_text:
            message += " The document has no pages of output."
    else:
        message = f"latexmk exited with code {exit_code}."
    return Diagnostic(None, None, "error", "compile_failed", message + " See the raw log.")


def _combined_log(output: str, truncated: bool, stem: str, tex_log: str | None) -> str:
    parts = [output.rstrip("\n")]
    if truncated:
        parts.append("[latexmk output was cut here]")
    if tex_log is not None:
        parts.append(f"===== {stem}.log =====")
        parts.append(tex_log.rstrip("\n"))
    return "\n".join(parts) + "\n"


def _cap_text(text: str, limit: int = MAX_LOG_TEXT_BYTES) -> str:
    """At most `limit` UTF-8 bytes: the start and the end, which hold the errors."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    budget = limit - 64  # room for the marker
    head = budget // 4
    tail = budget - head
    marker = f"\n[... {len(data) - head - tail} bytes omitted ...]\n".encode()
    return (data[:head] + marker + data[-tail:]).decode("utf-8", errors="ignore")
