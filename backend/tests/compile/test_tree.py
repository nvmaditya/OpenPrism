"""Scratch tree compiles and the shared compile cache, without TeX.

`CountingCompiler` stands in for the Compiler: it has `toolchain`,
`compile_dir` and `compile` with the Compiler's signatures and records each
call. The TeX lane (test_tree_tex.py) runs the same paths against real TeX.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prismtex.cancel import Cancelled, CancelToken
from prismtex.compile import Compiler, CompileResult, Diagnostic
from prismtex.compile.fake import FakeRule, FakeTreeCompiler, make_result
from prismtex.compile.tree import ScratchTreeCompiler, SourceTree, source_tree
from prismtex.errors import PathPolicyError, RootFileMissing
from prismtex.workspace import Engine, ProjectStore, content_version, encode_text

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], Any]

DOC = "\\documentclass{article}\n\\begin{document}\nA\n\\end{document}\n"
PNG = b"\x89PNG\r\n\x1a\nnot really an image"


class CountingCompiler:
    """The Compiler's tree-facing methods; records calls and what each one saw."""

    toolchain = "stub 1.0"

    def __init__(self, store: ProjectStore | None = None) -> None:
        self.store = store
        self.calls: list[tuple[str, Path | str]] = []
        self.seen: dict[str, bytes] = {}  # the files of the last compile_dir's src_dir
        self.diagnostics: tuple[Diagnostic, ...] = ()
        self.status: Any = "ok"
        self.delay_s = 0.0
        self.on_compile_dir: Callable[[Callable[[], bool] | None], None] | None = None

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
        should_stop: Callable[[], bool] | None = None,
    ) -> CompileResult:
        self.calls.append(("compile_dir", src_dir))
        self.seen = {
            path.relative_to(src_dir).as_posix(): path.read_bytes()
            for path in sorted(src_dir.rglob("*"))
            if path.is_file()
        }
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.on_compile_dir is not None:
            self.on_compile_dir(should_stop)
        diagnostics = tuple(
            Diagnostic(
                d.file.replace("{src}", src_dir.as_posix()) if d.file else None,
                d.line,
                d.severity,
                d.kind,
                d.message,
            )
            for d in self.diagnostics
        )
        return make_result(
            status=self.status,
            diagnostics=diagnostics,
            pdf_path=out_dir / "main.pdf",
            root_file=root_file,
            engine=engine,
            log_id=None,
        )

    def compile(
        self, project_id: str, *, root_file: str | None = None, engine: Engine | None = None
    ) -> CompileResult:
        self.calls.append(("compile", project_id))
        assert self.store is not None
        pdf = self.store.layout.out(project_id) / "main.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(f"%PDF user compile {len(self.calls)}".encode())
        return make_result(
            status="ok",
            pdf_path=pdf,
            root_file=root_file or "main.tex",
            log_id=f"log{len(self.calls)}",
        )


def _scratch(store: ProjectStore, pid: str, turn_id: str) -> Path:
    return store.layout.build(pid) / "scratch" / turn_id


@pytest.mark.anyio
async def test_tree_compile_cache_hit_on_an_identical_digest(blank_project: Project) -> None:
    store, pid = blank_project
    inner = CountingCompiler()
    tc = ScratchTreeCompiler(inner, store.layout)
    t1 = source_tree(store, pid, overlay={"main.tex": DOC})
    t2 = source_tree(store, pid, overlay={"main.tex": DOC})
    assert t1.digest == t2.digest
    r1 = await tc.compile(t1, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    r2 = await tc.compile(t2, purpose="proposal", cancel=CancelToken(), turn_id="t_1")
    assert [kind for kind, _ in inner.calls] == ["compile_dir"] and r2 is r1
    assert tc.peek(t1.digest) is r1
    src = inner.calls[0][1]
    assert isinstance(src, Path) and src.is_relative_to(_scratch(store, pid, "t_1"))
    assert r1.status == "ok"
    t3 = source_tree(store, pid, overlay={"main.tex": DOC.replace("A", "B")})
    assert t3.digest != t1.digest
    await tc.compile(t3, purpose="proposal", cancel=CancelToken(), turn_id="t_1")
    assert len(inner.calls) == 2


@pytest.mark.anyio
async def test_scratch_materializes_overlay_eols_and_links_assets_from_the_cache(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "sections/a.tex", "Section A.\n")
    put(store, pid, "gone.tex", "Deleted in the overlay.\n")
    store.write_bytes(pid, "fig.png", PNG)
    inner = CountingCompiler()
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(
        store,
        pid,
        overlay={"main.tex": DOC, "new.tex": "New\n", "gone.tex": None},
        eols={"main.tex": "crlf"},
    )
    await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert inner.seen == {
        "main.tex": DOC.replace("\n", "\r\n").encode(),
        "new.tex": b"New\n",
        "sections/a.tex": b"Section A.\n",
        "fig.png": PNG,
    }
    [(_, src)] = inner.calls
    assert isinstance(src, Path)
    cached = store.layout.build(pid) / "assets" / "fig.png"
    project_fig = store.layout.project_dir(pid) / "fig.png"
    assert not os.path.samefile(src / "fig.png", project_fig)
    assert os.path.samefile(src / "fig.png", cached)

    # A changed Asset refreshes its cache entry, and the old link keeps its bytes.
    store.write_bytes(pid, "fig.png", PNG + b" v2")
    tree2 = source_tree(store, pid, overlay={"main.tex": DOC})
    await tc.compile(tree2, purpose="proposal", cancel=CancelToken(), turn_id="t_1")
    assert inner.seen["fig.png"] == PNG + b" v2"
    assert cached.read_bytes() == PNG + b" v2"
    assert (src / "fig.png").read_bytes() == PNG
    assert project_fig.read_bytes() == PNG + b" v2"


def test_source_tree_inputs_are_the_content_versions_of_the_effective_bytes(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "crlf.tex", "x")
    store.write_bytes(pid, "crlf.tex", b"one\r\ntwo\r\n")
    put(store, pid, "sections/a.tex", "A\n")
    store.write_bytes(pid, "fig.png", PNG)
    tree = source_tree(store, pid, overlay={"crlf.tex": "one\ntwo\nthree\n", "new.tex": "N\n"})
    assert tree.inputs["main.tex"] == store.read(pid, "main.tex").version
    assert tree.inputs["sections/a.tex"] == store.read(pid, "sections/a.tex").version
    assert tree.eols == {"crlf.tex": "crlf", "new.tex": "lf"}
    assert tree.inputs["crlf.tex"] == content_version(b"one\r\ntwo\r\nthree\r\n")
    assert tree.inputs["new.tex"] == content_version(encode_text("N\n", "lf"))
    assert "fig.png" not in tree.inputs and set(tree.assets) == {"fig.png"}
    assert (tree.root_file, tree.engine) == ("main.tex", "pdflatex")

    # The digest covers each Asset's size and mtime, the root file and the engine.
    same = source_tree(store, pid, overlay={"crlf.tex": "one\ntwo\nthree\n", "new.tex": "N\n"})
    assert same.digest == tree.digest
    fig = store.layout.project_dir(pid) / "fig.png"
    stat = fig.stat()
    os.utime(fig, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    touched = source_tree(store, pid, overlay={"crlf.tex": "one\ntwo\nthree\n", "new.tex": "N\n"})
    assert touched.digest != tree.digest
    assert source_tree(store, pid, engine="xelatex").digest != source_tree(store, pid).digest
    other_root = source_tree(store, pid, root_file="sections/a.tex")
    assert other_root.root_file == "sections/a.tex"
    assert other_root.digest != source_tree(store, pid).digest


def test_source_tree_without_a_root_file_raises_root_file_missing(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    store.update(pid, root_file=None)
    with pytest.raises(RootFileMissing):
        source_tree(store, pid)


@pytest.mark.parametrize("bad", ["../escape.tex", "/abs.tex", ".git/config", ".prism/x.tex"])
def test_source_tree_refuses_an_overlay_path_outside_the_policy(
    blank_project: Project, bad: str
) -> None:
    store, pid = blank_project
    with pytest.raises(PathPolicyError):
        source_tree(store, pid, overlay={bad: "x"})


@pytest.mark.anyio
async def test_diagnostic_paths_are_mapped_back_to_project_relative(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "sections/a.tex", "A\n")
    inner = CountingCompiler()
    inner.diagnostics = (
        Diagnostic("sections/a.tex", 3, "error", "tex_error", "kept"),
        Diagnostic("{src}/sections/a.tex", 4, "error", "tex_error", "absolute inside"),
        Diagnostic("Sections/A.tex", 5, "error", "tex_error", "other case"),
        Diagnostic("not-in-tree.tex", 6, "warning", "latex_warning", "outside"),
        Diagnostic(None, None, "error", "fatal", "nowhere"),
    )
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    result = await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert [(d.file, d.line, d.message) for d in result.diagnostics] == [
        ("sections/a.tex", 3, "kept"),
        ("sections/a.tex", 4, "absolute inside"),
        ("sections/a.tex", 5, "other case"),
        (None, None, "outside"),
        (None, None, "nowhere"),
    ]


@pytest.mark.anyio
async def test_cleanup_removes_the_turn_scratch_dir_and_sweep_removes_leftovers(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    other = store.create(kind="blank").id
    tc = ScratchTreeCompiler(CountingCompiler(), store.layout)
    for project, turn in ((pid, "t_1"), (pid, "t_2"), (other, "t_3")):
        tree = source_tree(store, project, overlay={"main.tex": DOC + f"% {turn}\n"})
        await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id=turn)
    assert _scratch(store, pid, "t_1").is_dir()
    tc.cleanup(pid, "t_1")
    assert not _scratch(store, pid, "t_1").exists()
    assert _scratch(store, pid, "t_2").is_dir()
    tc.cleanup(pid, "t_1")  # already gone: nothing to do
    assert tc.sweep() == 2
    assert not _scratch(store, pid, "t_2").exists() and not _scratch(store, other, "t_3").exists()
    assert tc.sweep() == 0
    assert store.layout.project_dir(pid).is_dir()


def test_cleanup_refuses_a_turn_id_that_is_not_a_plain_name(blank_project: Project) -> None:
    store, pid = blank_project
    tc = ScratchTreeCompiler(CountingCompiler(), store.layout)
    for bad in ("..", "a/b", "", "a\\b"):
        with pytest.raises(ValueError):
            tc.cleanup(pid, bad)


def test_default_app_wraps_the_compiler_in_a_scratch_tree_compiler(client: TestClient) -> None:
    assert isinstance(client.app, FastAPI)
    tree_compiler = client.app.state.tree_compiler
    assert isinstance(tree_compiler, ScratchTreeCompiler)
    assert tree_compiler.compiler is client.app.state.compiler


def test_user_compile_route_goes_through_the_tree_compiler_with_an_empty_overlay(
    client: TestClient,
) -> None:
    assert isinstance(client.app, FastAPI)
    store: ProjectStore = client.app.state.store
    fake = FakeTreeCompiler(store)
    client.app.state.tree_compiler = fake
    pid = client.post("/api/projects", json={"kind": "blank"}).json()["id"]
    body = client.post(f"/api/projects/{pid}/compile", json={"engine": "xelatex"}).json()
    assert (body["status"], body["engine"], body["root_file"]) == ("ok", "xelatex", "main.tex")
    [(tree, purpose)] = fake.calls
    assert purpose == "user" and tree.overlay == {} and tree.project_id == pid
    assert (tree.root_file, tree.engine) == ("main.tex", "xelatex")
    assert tree.inputs == {"main.tex": store.read(pid, "main.tex").version}


@pytest.mark.anyio
async def test_user_compile_after_an_identical_proposal_compile_refreshes_out_dir(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    inner = CountingCompiler(store)
    tc = ScratchTreeCompiler(inner, store.layout)
    proposal = source_tree(store, pid, overlay={"main.tex": DOC})
    await tc.compile(proposal, purpose="proposal", cancel=CancelToken(), turn_id="t_1")
    put(store, pid, "main.tex", DOC)  # the user accepts the Proposal
    user_tree = source_tree(store, pid)
    assert user_tree.digest == proposal.digest
    result = await tc.compile(user_tree, purpose="user", cancel=CancelToken())
    assert [kind for kind, _ in inner.calls] == ["compile_dir", "compile"]
    assert result.pdf_path is not None
    assert result.pdf_path.is_relative_to(store.layout.out(pid))
    # The user result now serves the next Baseline of that tree.
    again = await tc.compile(proposal, purpose="baseline", cancel=CancelToken(), turn_id="t_2")
    assert again is result and len(inner.calls) == 2


@pytest.mark.anyio
async def test_user_compile_reuses_an_earlier_user_result_while_its_pdf_is_unchanged(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    inner = CountingCompiler(store)
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid)
    first = await tc.compile(tree, purpose="user", cancel=CancelToken())
    second = await tc.compile(tree, purpose="user", cancel=CancelToken())
    assert second is first and len(inner.calls) == 1
    # Another compile overwrote the PDF in build/<pid>/out: compile again.
    assert first.pdf_path is not None
    first.pdf_path.write_bytes(b"%PDF from some other compile, longer than before")
    third = await tc.compile(tree, purpose="user", cancel=CancelToken())
    assert third is not first and len(inner.calls) == 2


@pytest.mark.anyio
async def test_user_compile_refuses_an_overlay_and_scratch_compiles_need_a_turn_id(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    tc = ScratchTreeCompiler(CountingCompiler(store), store.layout)
    with pytest.raises(ValueError):
        await tc.compile(
            source_tree(store, pid, overlay={"main.tex": DOC}), purpose="user", cancel=CancelToken()
        )
    with pytest.raises(ValueError):
        await tc.compile(source_tree(store, pid), purpose="baseline", cancel=CancelToken())
    with pytest.raises(ValueError):
        await tc.compile(
            source_tree(store, pid), purpose="baseline", cancel=CancelToken(), turn_id="../x"
        )


@pytest.mark.anyio
async def test_scratch_compile_runs_while_a_user_compile_holds_the_project_lock(
    blank_project: Project, tmp_path: Path
) -> None:
    store, pid = blank_project
    empty = tmp_path / "no-tex-here"
    empty.mkdir()
    compiler = Compiler(store, base_env={"PATH": str(empty)})
    tc = ScratchTreeCompiler(compiler, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    with compiler._lock(pid):  # what a running user compile holds
        assert compiler.is_compiling(pid)
        with anyio.fail_after(30):
            result = await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert result.status == "unavailable"
    assert (_scratch(store, pid, "t_1") / tree.digest / "compile.log").is_file()


@pytest.mark.anyio
async def test_unavailable_and_timeout_results_are_not_cached(blank_project: Project) -> None:
    store, pid = blank_project
    inner = CountingCompiler()
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    for status in ("unavailable", "timeout"):
        inner.status = status
        await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
        assert tc.peek(tree.digest) is None
    inner.status = "failed"
    failed = await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert tc.peek(tree.digest) is failed and len(inner.calls) == 3


@pytest.mark.anyio
async def test_a_toolchain_change_never_reuses_a_result(blank_project: Project) -> None:
    store, pid = blank_project
    inner = CountingCompiler()
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    inner.toolchain = "stub 2.0"
    assert tc.peek(tree.digest) is None
    await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert len(inner.calls) == 2


@pytest.mark.anyio
async def test_the_cache_keeps_the_most_recently_used_entries(blank_project: Project) -> None:
    store, pid = blank_project
    tc = ScratchTreeCompiler(CountingCompiler(), store.layout, cache_size=2)
    trees = [source_tree(store, pid, overlay={"main.tex": DOC + f"% {n}\n"}) for n in range(3)]
    for tree in trees[:2]:
        await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert tc.peek(trees[0].digest) is not None  # now the most recently used
    await tc.compile(trees[2], purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert tc.peek(trees[1].digest) is None
    assert tc.peek(trees[0].digest) is not None and tc.peek(trees[2].digest) is not None


@pytest.mark.anyio
async def test_concurrent_compiles_of_one_tree_run_once(blank_project: Project) -> None:
    store, pid = blank_project
    inner = CountingCompiler()
    inner.delay_s = 0.3
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    results: list[CompileResult] = []

    async def run(purpose: Any) -> None:
        results.append(await tc.compile(tree, purpose=purpose, cancel=CancelToken(), turn_id="t_1"))

    async with anyio.create_task_group() as group:
        group.start_soon(run, "baseline")
        group.start_soon(run, "proposal")
    assert len(inner.calls) == 1 and results[0] is results[1]


@pytest.mark.anyio
async def test_a_project_file_changed_before_materializing_is_not_cached(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "sections/a.tex", "A\n")
    inner = CountingCompiler()
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    put(store, pid, "sections/a.tex", "Changed after the capture\n")
    await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert tc.peek(tree.digest) is None


@pytest.mark.anyio
async def test_cancel_before_or_during_a_compile_raises_cancelled_and_caches_nothing(
    blank_project: Project,
) -> None:
    store, pid = blank_project
    inner = CountingCompiler()
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    cancelled = CancelToken()
    cancelled.set()
    with pytest.raises(Cancelled):
        await tc.compile(tree, purpose="baseline", cancel=cancelled, turn_id="t_1")
    assert inner.calls == []

    token = CancelToken()
    seen_stop: list[bool] = []

    def cancel_midway(should_stop: Callable[[], bool] | None) -> None:
        assert should_stop is not None
        seen_stop.append(should_stop())
        token.set()  # the Turn is cancelled while TeX runs
        seen_stop.append(should_stop())

    inner.on_compile_dir = cancel_midway
    with pytest.raises(Cancelled):
        await tc.compile(tree, purpose="baseline", cancel=token, turn_id="t_1")
    assert seen_stop == [False, True]
    assert tc.peek(tree.digest) is None


@pytest.mark.anyio
async def test_a_cancelled_task_asks_the_running_compile_to_stop(blank_project: Project) -> None:
    store, pid = blank_project
    inner = CountingCompiler()
    stopped = threading.Event()

    def wait_for_stop(should_stop: Callable[[], bool] | None) -> None:
        assert should_stop is not None
        deadline = time.monotonic() + 10
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(0.01)
        if should_stop():
            stopped.set()

    inner.on_compile_dir = wait_for_stop
    tc = ScratchTreeCompiler(inner, store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": DOC})
    with anyio.move_on_after(0.3):
        await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert stopped.wait(10)  # the compile's thread saw the stop


def test_make_result_builds_a_compile_result_for_each_status(tmp_path: Path) -> None:
    ok = make_result(pdf_path=str(tmp_path / "main.pdf"))
    assert (ok.status, ok.ok, ok.exit_code, ok.pdf_path) == ("ok", True, 0, tmp_path / "main.pdf")
    assert (ok.root_file, ok.engine, ok.log_id, ok.duration_ms) == ("main.tex", "pdflatex", "l", 1)
    assert make_result(status="failed").exit_code == 1
    assert make_result(status="timeout").exit_code is None
    unavailable = make_result(status="unavailable", engine="xelatex", root_file="thesis.tex")
    assert (unavailable.ok, unavailable.engine, unavailable.root_file) == (
        False,
        "xelatex",
        "thesis.tex",
    )


@pytest.mark.anyio
async def test_fake_tree_compiler_rules_unless_fatal_and_unavailable(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "refs.bib", "@book{real, title={R}}\n")
    rules = (
        FakeRule(r"\\badmacro", "undefined_control_sequence", "Undefined control sequence {match}"),
        FakeRule(
            r"\\cite\{ghost\}",
            "undefined_citation",
            "Citation `ghost' undefined",
            severity="warning",
            unless=r"@\w+\{ghost,",
        ),
        FakeRule(r"\\input\{missing\}", "fatal", "Emergency stop.", fatal=True, no_pdf=True),
    )
    fake = FakeTreeCompiler(store, *rules)
    body = "\\documentclass{article}\n\\begin{document}\n\\badmacro \\cite{ghost}\n"
    tree = source_tree(store, pid, overlay={"main.tex": body + "\\end{document}\n"})
    result = await fake.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert [(d.file, d.line, d.severity, d.kind, d.message) for d in result.diagnostics] == [
        (
            "main.tex",
            3,
            "error",
            "undefined_control_sequence",
            "Undefined control sequence \\badmacro",
        ),
        ("main.tex", 3, "warning", "undefined_citation", "Citation `ghost' undefined"),
    ]
    assert result.status == "failed" and result.pdf_path is not None

    # `unless` turns a rule off when its pattern matches anywhere in the tree.
    wired = source_tree(
        store,
        pid,
        overlay={
            "main.tex": "\\cite{ghost}\n",
            "refs.bib": "@book{real, title={R}}\n@book{ghost, title={G}}\n",
        },
    )
    clean = await fake.compile(wired, purpose="proposal", cancel=CancelToken(), turn_id="t_1")
    assert (clean.status, clean.diagnostics) == ("ok", ())

    # `fatal` stops scanning (a masked Baseline); `no_pdf` drops the PDF.
    masked = source_tree(
        store, pid, overlay={"main.tex": "\\input{missing}\n\\badmacro\n", "z.tex": "\\badmacro\n"}
    )
    stopped = await fake.compile(masked, purpose="proposal", cancel=CancelToken(), turn_id="t_1")
    assert [(d.kind, d.line) for d in stopped.diagnostics] == [("fatal", 1)]
    assert stopped.pdf_path is None and stopped.status == "failed"

    # The fake caches like the real one, including the user-purpose rule.
    assert await fake.compile(tree, purpose="proposal", cancel=CancelToken(), turn_id="t_2") is (
        result
    )
    assert fake.peek(tree.digest) is result
    assert [purpose for _, purpose in fake.calls] == ["baseline", "proposal", "proposal"]
    put(store, pid, "main.tex", body + "\\end{document}\n")
    user = await fake.compile(source_tree(store, pid), purpose="user", cancel=CancelToken())
    assert user is not result and [p for _, p in fake.calls][-1] == "user"
    assert await fake.compile(source_tree(store, pid), purpose="user", cancel=CancelToken()) is user

    unavailable = FakeTreeCompiler(store, *rules, unavailable=True)
    gone = await unavailable.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_1")
    assert gone.status == "unavailable" and gone.pdf_path is None
    assert [d.kind for d in gone.diagnostics] == ["tex_missing"]
    assert unavailable.peek(tree.digest) is None

    token = CancelToken()
    token.set()
    with pytest.raises(Cancelled):
        await fake.compile(tree, purpose="baseline", cancel=token, turn_id="t_1")


def test_fake_tree_compiler_satisfies_the_tree_compiler_protocol(blank_project: Project) -> None:
    from prismtex.compile.tree import TreeCompiler

    store, _ = blank_project
    compilers: list[TreeCompiler] = [
        FakeTreeCompiler(store),
        ScratchTreeCompiler(CountingCompiler(), store.layout),
    ]
    assert all(c.peek("0" * 64) is None for c in compilers)


def test_source_tree_is_a_frozen_value(blank_project: Project) -> None:
    store, pid = blank_project
    tree = source_tree(store, pid)
    assert isinstance(tree, SourceTree)
    with pytest.raises(AttributeError):
        tree.digest = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        tree.inputs["main.tex"] = "x"  # type: ignore[index]
