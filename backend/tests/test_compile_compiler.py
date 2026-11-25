"""Compiler without real TeX: validation, status rules, stale outputs, logs.

A fake `latexmk` (a small Python script on a private PATH) stands in for TeX
where the behaviour depends on what latexmk writes and prints. The TeX lane
(test_tex_lane.py) runs the same paths against real TeX.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from prismtex.compile import (
    KEEP_LOGS,
    MAX_LOG_TEXT_BYTES,
    TEX_MISSING_MESSAGE,
    CompileLogStore,
    Compiler,
    build_latexmk_argv,
)
from prismtex.errors import (
    InvalidRequest,
    LogNotFound,
    ProjectNotFound,
    RootFileInvalid,
    RootFileMissing,
)
from prismtex.workspace import ProjectStore

FIXTURES = Path(__file__).parent / "fixtures" / "texlogs"
ERRORS_LOG = (FIXTURES / "errors.log.txt").read_text(encoding="utf-8")
WARNINGS_LOG = (FIXTURES / "warnings.log.txt").read_text(encoding="utf-8")
CLEAN_LOG = (FIXTURES / "paper_ok.log.txt").read_text(encoding="utf-8")
SYNTAX_BLG = (FIXTURES / "bib_syntax.blg").read_text(encoding="utf-8")
UP_TO_DATE = "Latexmk: All targets (/data/build/p/out/main.pdf) are up-to-date\n"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake latexmk is a script")


def _empty_path(tmp_path: Path) -> dict[str, str]:
    empty = tmp_path / "no-tex-here"
    empty.mkdir(exist_ok=True)
    return {"PATH": str(empty)}


# -- no TeX needed -------------------------------------------------------------


def test_missing_latexmk_is_unavailable(store: ProjectStore, tmp_path: Path) -> None:
    info = store.create(kind="blank")
    compiler = Compiler(store, base_env=_empty_path(tmp_path))
    result = compiler.compile(info.id)
    assert result.status == "unavailable"
    assert not result.ok
    assert [(d.kind, d.severity, d.message) for d in result.diagnostics] == [
        ("tex_missing", "error", TEX_MISSING_MESSAGE)
    ]
    assert result.pdf_path is None
    assert result.exit_code is None
    assert result.log_id is not None
    [meta] = compiler.logs(info.id)
    assert meta["status"] == "unavailable"
    assert meta["errors"] == 1
    assert TEX_MISSING_MESSAGE in compiler.get_log(info.id, result.log_id)["log"]


def test_unknown_project(store: ProjectStore) -> None:
    compiler = Compiler(store)
    for call in (
        lambda: compiler.compile("0" * 32),
        lambda: compiler.logs("0" * 32),
        lambda: compiler.get_log("0" * 32, "1" * 32),
    ):
        with pytest.raises(ProjectNotFound):
            call()


def test_project_without_root_file(store: ProjectStore) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "no LaTeX here")
    buffer.seek(0)
    info = store.import_zip(buffer)
    assert info.root_file is None
    with pytest.raises(RootFileMissing):
        Compiler(store).compile(info.id)


@pytest.mark.parametrize(
    ("root_file", "reason"),
    [
        ("../outside.tex", "path_denied"),
        ("/etc/passwd.tex", "path_denied"),
        ("-shell-escape.tex", "leading_dash"),
        ("notes.txt", "not_tex"),
        ("missing.tex", "not_found"),
        ("a`touch pwned`b.tex", "bad_char"),
        ("a!b.tex", "bad_char"),
        ("a$b.tex", "bad_char"),
    ],
)
def test_root_file_override_is_validated(
    store: ProjectStore, tmp_path: Path, root_file: str, reason: str
) -> None:
    info = store.create(kind="blank")
    project = store.project_dir(info.id)
    for name in ("-shell-escape.tex", "notes.txt", "a`touch pwned`b.tex", "a!b.tex"):
        (project / name).write_text("\\documentclass{article}\n", encoding="utf-8")
    compiler = Compiler(store, base_env=_empty_path(tmp_path))
    with pytest.raises(RootFileInvalid) as caught:
        compiler.compile(info.id, root_file=root_file)
    assert caught.value.reason == reason
    assert not (project / "pwned").exists()


def test_engine_is_validated(store: ProjectStore) -> None:
    info = store.create(kind="blank")
    with pytest.raises(InvalidRequest) as caught:
        Compiler(store).compile(info.id, engine="tex")  # pyright: ignore[reportArgumentType]
    assert caught.value.extra["field"] == "engine"


def test_out_dir_inside_the_sources_is_refused(store: ProjectStore) -> None:
    info = store.create(kind="blank")
    project = store.project_dir(info.id)
    with pytest.raises(ValueError):
        Compiler(store).compile_dir(
            project, project_id=info.id, root_file="main.tex", engine="pdflatex", out_dir=project
        )
    with pytest.raises(ValueError):
        Compiler(store).compile_dir(
            project,
            project_id=info.id,
            root_file="main.tex",
            engine="pdflatex",
            out_dir=project / "build",
        )


def test_get_log_refuses_bad_ids(store: ProjectStore, tmp_path: Path) -> None:
    info = store.create(kind="blank")
    compiler = Compiler(store, base_env=_empty_path(tmp_path))
    compiler.compile(info.id)
    for log_id in ("../../project", "f" * 32, "LOG", ""):
        with pytest.raises(LogNotFound):
            compiler.get_log(info.id, log_id)


def test_log_store_keeps_the_newest(store: ProjectStore) -> None:
    info = store.create(kind="blank")
    logs = CompileLogStore(store.layout)
    ids = [
        logs.save(
            info.id,
            status="ok",
            engine="pdflatex",
            root_file="main.tex",
            duration_ms=n,
            errors=0,
            warnings=n,
            text=f"log {n}",
        )
        for n in range(KEEP_LOGS + 5)
    ]
    listed = logs.list(info.id)
    assert [meta["log_id"] for meta in listed] == ids[::-1][:KEEP_LOGS]
    assert set(listed[0]) == {
        "log_id",
        "created_at",
        "status",
        "engine",
        "root_file",
        "duration_ms",
        "errors",
        "warnings",
    }
    files = sorted(path.name for path in store.layout.logs(info.id).iterdir())
    assert files == sorted([f"{i}.json" for i in ids[5:]] + [f"{i}.log" for i in ids[5:]])
    newest = logs.get(info.id, ids[-1])
    assert newest["log"] == f"log {KEEP_LOGS + 4}"
    with pytest.raises(LogNotFound):
        logs.get(info.id, ids[0])


def test_log_without_metadata_is_not_listed(store: ProjectStore) -> None:
    info = store.create(kind="blank")
    folder = store.layout.logs(info.id)
    folder.mkdir(parents=True)
    (folder / f"{'a' * 32}.log").write_text("interrupted save", encoding="utf-8")
    (folder / f"{'b' * 32}.json").write_text("{not json", encoding="utf-8")
    assert CompileLogStore(store.layout).list(info.id) == []


# -- with a fake latexmk -------------------------------------------------------

FAKE_LATEXMK = """#!{python}
import json, os, sys, time
from pathlib import Path

behaviour = json.loads({behaviour!r})
args = sys.argv[1:]
out = Path(next(a for a in args if a.startswith("-outdir="))[len("-outdir="):])
stem = Path(args[-1]).stem
record = behaviour.get("record")
if record:
    with open(record, "a") as f:
        f.write(json.dumps({{"argv": args, "cwd": os.getcwd(), "env": dict(os.environ),
                             "start": time.monotonic()}}) + "\\n")
for suffix in ("log", "blg"):
    if behaviour.get(suffix) is not None:
        (out / f"{{stem}}.{{suffix}}").write_text(behaviour[suffix])
if behaviour.get("pdf"):
    (out / f"{{stem}}.pdf").write_bytes(b"%PDF-1.5 fake")
sys.stdout.write(behaviour.get("output", ""))
sys.stdout.flush()
time.sleep(behaviour.get("sleep", 0))
if record:
    with open(record, "a") as f:
        f.write(json.dumps({{"end": time.monotonic()}}) + "\\n")
sys.exit(behaviour.get("exit", 0))
"""

MakeCompiler = Callable[..., Compiler]


@pytest.fixture
def fake_tex(store: ProjectStore, tmp_path: Path) -> MakeCompiler:
    """A Compiler whose PATH holds only a fake latexmk with the given behaviour."""

    def make(*, timeout_s: float = 30.0, base: dict[str, str] | None = None, **behaviour: Any):
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "latexmk"
        script.write_text(
            FAKE_LATEXMK.format(python=sys.executable, behaviour=json.dumps(behaviour)),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return Compiler(store, timeout_s=timeout_s, base_env={**(base or {}), "PATH": str(bin_dir)})

    return make


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@posix_only
def test_runs_the_hardened_argv_in_the_project_folder(
    store: ProjectStore, fake_tex: MakeCompiler, tmp_path: Path
) -> None:
    info = store.create(kind="blank")
    record = tmp_path / "record.jsonl"
    compiler = fake_tex(
        record=str(record), pdf=True, log=CLEAN_LOG, base={"OPENAI_API_KEY": "sk-secret"}
    )
    compiler.compile(info.id, engine="lualatex")
    [call, _end] = _records(record)
    out = store.layout.out(info.id)
    assert call["argv"] == build_latexmk_argv("main.tex", "lualatex", out)[1:]
    assert Path(call["cwd"]) == store.project_dir(info.id).resolve()
    env = call["env"]
    assert (env["openin_any"], env["openout_any"], env["shell_escape"]) == ("p", "p", "f")
    assert env["TEXMFOUTPUT"] == str(out)
    assert "OPENAI_API_KEY" not in env


@posix_only
def test_ok_when_the_pdf_is_written_and_no_errors(
    store: ProjectStore, fake_tex: MakeCompiler
) -> None:
    info = store.create(kind="blank")
    compiler = fake_tex(pdf=True, log=WARNINGS_LOG, output="latexmk says hi\n")
    result = compiler.compile(info.id)
    assert result.status == "ok"
    assert result.ok
    assert result.pdf_path == store.layout.out(info.id) / "main.pdf"
    assert result.exit_code == 0
    assert {d.kind for d in result.diagnostics} >= {"undefined_citation", "bad_box"}
    assert result.log_text.startswith("latexmk says hi\n===== main.log =====\n")
    assert result.log_id is not None
    [meta] = compiler.logs(info.id)
    assert (meta["status"], meta["errors"], meta["warnings"]) == ("ok", 0, 9)
    assert meta["root_file"] == "main.tex"
    assert meta["engine"] == "pdflatex"


@posix_only
def test_failed_with_diagnostics_from_the_log(store: ProjectStore, fake_tex: MakeCompiler) -> None:
    info = store.create(kind="blank")
    result = fake_tex(log=ERRORS_LOG, exit=12).compile(info.id)
    assert result.status == "failed"
    assert result.pdf_path is None
    first = result.diagnostics[0]
    assert (first.kind, first.file, first.line) == (
        "undefined_control_sequence",
        "sections/x.tex",
        3,
    )


@posix_only
def test_errors_fail_the_compile_even_with_a_pdf_and_exit_0(
    store: ProjectStore, fake_tex: MakeCompiler
) -> None:
    info = store.create(kind="blank")
    result = fake_tex(log=ERRORS_LOG, pdf=True, exit=0).compile(info.id)
    assert result.status == "failed"
    assert result.pdf_path is not None


@posix_only
def test_outputs_left_by_an_earlier_run_are_ignored(
    store: ProjectStore, fake_tex: MakeCompiler
) -> None:
    info = store.create(kind="blank")
    out = store.layout.out(info.id)
    out.mkdir(parents=True)
    (out / "main.pdf").write_bytes(b"%PDF old")
    (out / "main.log").write_text(ERRORS_LOG, encoding="utf-8")
    result = fake_tex(exit=12, output="Latexmk: something went wrong\n").compile(info.id)
    assert result.status == "failed"
    assert result.pdf_path is None
    assert [(d.kind, d.message) for d in result.diagnostics] == [
        (
            "compile_failed",
            "latexmk exited with code 12 before TeX wrote a log. See the raw log.",
        )
    ]
    assert "===== main.log" not in result.log_text


@posix_only
def test_up_to_date_outputs_count_as_this_run(store: ProjectStore, fake_tex: MakeCompiler) -> None:
    info = store.create(kind="blank")
    out = store.layout.out(info.id)
    out.mkdir(parents=True)
    (out / "main.pdf").write_bytes(b"%PDF current")
    (out / "main.log").write_text(WARNINGS_LOG, encoding="utf-8")
    result = fake_tex(output=UP_TO_DATE).compile(info.id)
    assert result.status == "ok"
    assert result.pdf_path == out / "main.pdf"
    assert any(d.kind == "undefined_citation" for d in result.diagnostics)


@posix_only
def test_an_old_pdf_is_not_reported_after_an_unchanged_failed_rerun(
    store: ProjectStore, fake_tex: MakeCompiler
) -> None:
    # latexmk says "up-to-date" and exits 12 when the sources have not
    # changed since a failed run; the PDF on disk is from before that.
    info = store.create(kind="blank")
    out = store.layout.out(info.id)
    out.mkdir(parents=True)
    (out / "main.pdf").write_bytes(b"%PDF from an earlier good run")
    (out / "main.log").write_text(ERRORS_LOG, encoding="utf-8")
    result = fake_tex(output=UP_TO_DATE, exit=12).compile(info.id)
    assert result.status == "failed"
    assert result.pdf_path is None
    assert result.diagnostics[0].kind == "undefined_control_sequence"


@posix_only
@pytest.mark.parametrize(
    ("blg_written", "log_mentions_bbl", "parsed"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_blg_is_read_only_when_it_belongs_to_this_run(
    store: ProjectStore,
    fake_tex: MakeCompiler,
    blg_written: bool,
    log_mentions_bbl: bool,
    parsed: bool,
) -> None:
    info = store.create(kind="blank")
    out = store.layout.out(info.id)
    out.mkdir(parents=True)
    if not blg_written:
        (out / "main.blg").write_text(SYNTAX_BLG, encoding="utf-8")
    log = CLEAN_LOG if log_mentions_bbl else "(./main.tex)\nNo pages of output.\n"
    assert ("main.bbl" in log) is log_mentions_bbl
    compiler = fake_tex(pdf=True, log=log, blg=SYNTAX_BLG if blg_written else None)
    result = compiler.compile(info.id)
    assert any(d.kind == "bibtex_error" for d in result.diagnostics) is parsed


@posix_only
def test_timeout(store: ProjectStore, fake_tex: MakeCompiler) -> None:
    info = store.create(kind="blank")
    result = fake_tex(timeout_s=1, pdf=True, sleep=30).compile(info.id)
    assert result.status == "timeout"
    assert result.exit_code is None
    assert [(d.kind, d.message) for d in result.diagnostics] == [
        ("timeout", "The compile was stopped after 1 seconds.")
    ]
    assert result.pdf_path is None
    # The fake wrote a PDF before it hung: a killed TeX leaves a truncated one.
    assert not (store.layout.out(info.id) / "main.pdf").exists()


@posix_only
def test_no_pdf_is_explained(store: ProjectStore, fake_tex: MakeCompiler) -> None:
    info = store.create(kind="blank")
    result = fake_tex(log="(./main.tex)\nNo pages of output.\n").compile(info.id)
    assert result.status == "failed"
    assert [(d.kind, d.message) for d in result.diagnostics] == [
        (
            "compile_failed",
            "No PDF was produced. The document has no pages of output. See the raw log.",
        )
    ]


@posix_only
def test_refused_file_access_is_reported(store: ProjectStore, fake_tex: MakeCompiler) -> None:
    info = store.create(kind="blank")
    output = "pdflatex: Not reading from /etc/hostname (openin_any = p).\n"
    result = fake_tex(pdf=True, log=CLEAN_LOG, output=output).compile(info.id)
    assert [(d.kind, d.severity) for d in result.diagnostics] == [("access_denied", "warning")]
    assert result.status == "ok"


@posix_only
def test_log_text_is_capped(store: ProjectStore, fake_tex: MakeCompiler) -> None:
    info = store.create(kind="blank")
    huge = CLEAN_LOG + ("x" * 99 + "\n") * 50_000
    result = fake_tex(pdf=True, log=huge, output="START\n").compile(info.id)
    assert len(result.log_text.encode("utf-8")) <= MAX_LOG_TEXT_BYTES
    assert result.log_text.startswith("START\n")
    assert "bytes omitted" in result.log_text
    assert result.log_text.rstrip().endswith("x" * 99)


@posix_only
def test_compile_dir_writes_log_path(
    store: ProjectStore, fake_tex: MakeCompiler, tmp_path: Path
) -> None:
    info = store.create(kind="blank")
    log_path = tmp_path / "scratch" / "logs" / "run.log"
    result = fake_tex(pdf=True, log=CLEAN_LOG).compile_dir(
        store.project_dir(info.id),
        project_id=info.id,
        root_file="main.tex",
        engine="pdflatex",
        out_dir=tmp_path / "scratch" / "out",
    )
    assert result.status == "ok"
    assert result.log_id is None
    assert not log_path.exists()
    fake_tex(pdf=True, log=CLEAN_LOG).compile_dir(
        store.project_dir(info.id),
        project_id=info.id,
        root_file="main.tex",
        engine="pdflatex",
        out_dir=tmp_path / "scratch" / "out",
        log_path=log_path,
    )
    assert "===== main.log =====" in log_path.read_text(encoding="utf-8")
    assert store.layout.logs(info.id).exists() is False


@posix_only
def test_override_root_file_and_engine(
    store: ProjectStore, fake_tex: MakeCompiler, tmp_path: Path
) -> None:
    info = store.create(kind="blank")
    (store.project_dir(info.id) / "other.tex").write_text("x", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    result = fake_tex(record=str(record), pdf=True, log=CLEAN_LOG).compile(
        info.id, root_file="other.tex", engine="xelatex"
    )
    assert (result.root_file, result.engine) == ("other.tex", "xelatex")
    assert result.pdf_path == store.layout.out(info.id) / "other.pdf"
    argv = _records(record)[0]["argv"]
    assert "-pdfxe" in argv
    assert argv[-1] == "other.tex"
    assert store.get(info.id).root_file == "main.tex"


@posix_only
def test_one_compile_per_project_at_a_time(
    store: ProjectStore, fake_tex: MakeCompiler, tmp_path: Path
) -> None:
    info = store.create(kind="blank")
    record = tmp_path / "record.jsonl"
    compiler = fake_tex(record=str(record), pdf=True, log=CLEAN_LOG, sleep=0.5)
    threads = [threading.Thread(target=compiler.compile, args=(info.id,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events = _records(record)
    starts = [e["start"] for e in events if "start" in e]
    ends = [e["end"] for e in events if "end" in e]
    assert len(starts) == len(ends) == 2
    assert min(ends) <= max(starts), "the second compile started before the first ended"
    assert len(compiler.logs(info.id)) == 2


@posix_only
def test_deleted_project_does_not_get_its_state_back(
    store: ProjectStore, fake_tex: MakeCompiler, tmp_path: Path
) -> None:
    info = store.create(kind="blank")
    compiler = fake_tex(pdf=True, log=CLEAN_LOG, sleep=0.5)
    worker = threading.Thread(target=compiler.compile, args=(info.id,))
    worker.start()
    threading.Event().wait(0.2)
    store.delete(info.id)
    worker.join()
    assert not store.layout.state(info.id).exists()
