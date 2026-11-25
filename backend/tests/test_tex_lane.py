"""The TeX lane (`pytest -m tex`): the hardened compile against real TeX.

These tests fail, never skip, when latexmk (or, for its test, lualatex) is
missing: a lane that silently skips proves nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prismtex.compile import Compiler, CompileResult
from prismtex.settings import Settings
from prismtex.workspace import ProjectStore

pytestmark = [pytest.mark.tex, pytest.mark.usefixtures("require_latexmk")]

MakeClient = Callable[..., TestClient]

ARTICLE = "\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"


@pytest.fixture
def require_latexmk() -> None:
    assert shutil.which("latexmk"), "latexmk is not on PATH; the TeX lane needs TeX Live or MiKTeX"


@pytest.fixture
def compiler(store: ProjectStore) -> Compiler:
    return Compiler(store, timeout_s=120)


@pytest.fixture
def outside() -> Iterator[Path]:
    """A folder outside the data dir whose name TeX can read without escaping."""
    path = Path(os.path.realpath(os.sep + "tmp" if os.name != "nt" else os.environ["TEMP"]))
    path = path / f"prismtex-outside-{uuid.uuid4().hex}"
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _project(store: ProjectStore, files: dict[str, str], kind: str = "blank") -> str:
    info = store.create(kind=kind)  # pyright: ignore[reportArgumentType]
    for rel, text in files.items():
        store.write(info.id, rel, text, None)
    return info.id


def _article(body: str) -> str:
    return ARTICLE.format(body=body)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


def _kinds(result: CompileResult) -> set[str]:
    return {d.kind for d in result.diagnostics}


# -- templates -----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["blank", "paper", "resume"])
def test_templates_compile_ok(store: ProjectStore, compiler: Compiler, kind: str) -> None:
    info = store.create(kind=kind)  # pyright: ignore[reportArgumentType]
    result = compiler.compile(info.id)
    assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
    assert result.pdf_path is not None and result.pdf_path.read_bytes().startswith(b"%PDF")
    assert not [d for d in result.diagnostics if d.severity == "error"]
    assert not _kinds(result) & {"undefined_citation", "undefined_reference"}
    out = store.layout.out(info.id)
    assert (out / "main.synctex.gz").is_file()
    if kind == "paper":
        # The citations resolved through BibTeX.
        bbl = (out / "main.bbl").read_text(encoding="utf-8")
        assert bbl.count("\\bibitem") == 3


def test_unchanged_recompile_is_still_ok(store: ProjectStore, compiler: Compiler) -> None:
    info = store.create(kind="paper")
    first = compiler.compile(info.id)
    second = compiler.compile(info.id)
    assert (first.status, second.status) == ("ok", "ok")
    assert second.pdf_path == first.pdf_path
    assert len(compiler.logs(info.id)) == 2


def test_lualatex_compiles(store: ProjectStore, compiler: Compiler) -> None:
    info = store.create(kind="blank")
    result = compiler.compile(info.id, engine="lualatex")
    assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
    assert result.engine == "lualatex"


def test_root_file_with_a_space_in_a_subfolder(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(store, {"paper/my paper.tex": _article("Hello.")})
    result = compiler.compile(pid, root_file="paper/my paper.tex")
    assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
    assert result.pdf_path == store.layout.out(pid) / "my paper.pdf"


# -- no artifacts in the project folder ----------------------------------------


def test_project_folder_is_byte_identical_after_compiles(
    store: ProjectStore, compiler: Compiler
) -> None:
    info = store.create(kind="paper")
    project = store.project_dir(info.id)
    before = _snapshot(project)
    assert compiler.compile(info.id).status == "ok"
    assert compiler.compile(info.id).status == "ok"
    assert _snapshot(project) == before
    # A failing compile leaves nothing behind either.
    store.write(info.id, "sections/method.tex", "\\undefinedmacro\n", None)
    middle = _snapshot(project)
    assert compiler.compile(info.id).status == "failed"
    assert _snapshot(project) == middle
    assert sorted(p.name for p in store.layout.projects.iterdir()) == [info.id]


# -- the hardening -------------------------------------------------------------


def test_input_of_an_absolute_path_is_blocked(
    store: ProjectStore, compiler: Compiler, outside: Path
) -> None:
    (outside / "secret.tex").write_text("PRISMTEX-SECRET-ABSOLUTE\n", encoding="utf-8")
    target = (outside / "secret").as_posix()
    pid = _project(store, {"main.tex": _article(f"\\input{{{target}}}")})
    result = compiler.compile(pid)
    assert result.status == "failed"
    assert result.pdf_path is None
    assert "PRISMTEX-SECRET-ABSOLUTE" not in result.log_text
    assert {"missing_file", "access_denied"} <= _kinds(result)


def test_input_of_a_system_file_is_blocked(store: ProjectStore, compiler: Compiler) -> None:
    candidates = [Path("/etc/hostname"), Path("/etc/passwd"), Path("C:/Windows/win.ini")]
    target = next((path for path in candidates if path.is_file()), None)
    assert target is not None, "no system file to try reading"
    pid = _project(store, {"main.tex": _article(f"\\input{{{target.as_posix()}}}")})
    result = compiler.compile(pid)
    assert result.status == "failed"
    assert "missing_file" in _kinds(result)
    if sys.platform != "win32":  # the refusal message is TeX Live's kpathsea wording
        assert any(
            d.kind == "access_denied" and target.as_posix() in d.message for d in result.diagnostics
        )


def test_input_of_a_parent_path_is_blocked(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(store, {"main.tex": _article("\\input{../outside}")})
    neighbour = store.layout.projects / "outside.tex"
    neighbour.write_text("PRISMTEX-SECRET-PARENT\n", encoding="utf-8")
    try:
        result = compiler.compile(pid)
    finally:
        neighbour.unlink()
    assert result.status == "failed"
    assert "PRISMTEX-SECRET-PARENT" not in result.log_text
    assert "missing_file" in _kinds(result)


def test_write18_is_disabled(store: ProjectStore, compiler: Compiler) -> None:
    body = "\\immediate\\write18{touch pwned-write18}\nHello."
    pid = _project(store, {"main.tex": _article(body)})
    result = compiler.compile(pid)
    assert result.status == "ok"
    assert "runsystem(touch pwned-write18)...disabled" in result.log_text
    for folder in (store.project_dir(pid), store.layout.out(pid)):
        assert not (folder / "pwned-write18").exists()


def test_openout_outside_is_blocked(store: ProjectStore, compiler: Compiler, outside: Path) -> None:
    target = (outside / "written.txt").as_posix()
    body = (
        "\\newwrite\\out\n"
        f"\\immediate\\openout\\out={target}\n"
        "\\immediate\\write\\out{pwned}\n"
        "\\immediate\\closeout\\out\n"
        "Hello."
    )
    pid = _project(store, {"main.tex": _article(body)})
    result = compiler.compile(pid)
    assert result.status == "failed"
    assert not (outside / "written.txt").exists()
    assert "access_denied" in _kinds(result)

    body = body.replace(target, "../escaped.txt")
    store.write(pid, "main.tex", _article(body), None)
    assert compiler.compile(pid).status == "failed"
    assert not (store.layout.projects / "escaped.txt").exists()


def test_project_latexmkrc_is_ignored(store: ProjectStore, compiler: Compiler) -> None:
    rc = '$pdflatex = "touch rc-pdflatex; pdflatex %O %S";\nsystem("touch rc-system");\n'
    pid = _project(store, {"main.tex": _article("Hello.")})
    project = store.project_dir(pid)
    (project / "latexmkrc").write_text(rc, encoding="utf-8")
    (project / ".latexmkrc").write_text(rc, encoding="utf-8")
    result = compiler.compile(pid)
    assert result.status == "ok"
    for folder in (project, store.layout.out(pid)):
        for name in ("rc-pdflatex", "rc-system"):
            assert not (folder / name).exists(), name


LUA_PROBE = r"""
\directlua{
  local function probe(label, handle)
    texio.write_nl("log", "PRISMTEST " .. label .. "=" .. (handle and "OPENED" or "BLOCKED"))
    if handle then handle:close() end
  end
  probe("read", io.open("SECRET", "r"))
  probe("write", io.open("TARGET", "w"))
  probe("parent", io.open("../lua-escaped.txt", "w"))
  texio.write_nl("log", "PRISMTEST home=" .. tostring(os.getenv("HOME") ~= nil))
  texio.write_nl("log", "PRISMTEST getenv=" .. tostring(os.getenv("PRISMTEX_TEST_SECRET")))
  pcall(os.execute, "touch lua-execute")
  pcall(io.popen, "touch lua-popen")
}
Hello.
"""


def test_lualatex_cannot_reach_outside_or_read_the_environment(
    store: ProjectStore, outside: Path
) -> None:
    assert shutil.which("lualatex"), "lualatex is not on PATH; this test needs it"
    (outside / "secret.txt").write_text("PRISMTEX-SECRET-LUA\n", encoding="utf-8")
    probe = LUA_PROBE.replace("SECRET", (outside / "secret.txt").as_posix())
    probe = probe.replace("TARGET", (outside / "lua-written.txt").as_posix())
    pid = _project(store, {"main.tex": _article(probe)})
    compiler = Compiler(store, base_env={**os.environ, "PRISMTEX_TEST_SECRET": "lua-secret-value"})
    result = compiler.compile(pid, engine="lualatex")
    log = result.log_text
    # --safer replaces os.execute and io.popen with stubs that say so. LuaTeX
    # 1.17 disables io.open too; a LuaTeX that only limits it to reading
    # still runs kpathsea's checks, so the probes below are the real test.
    for name in ("os.execute", "io.popen"):
        assert f'safer option set, function "{name}" is disabled' in log
    assert re.search(r'safer option set, function "io\.open" is (disabled|limited)', log)
    assert "PRISMTEST read=BLOCKED" in log
    assert "PRISMTEST write=BLOCKED" in log
    assert "PRISMTEST parent=BLOCKED" in log
    assert "PRISMTEST home=true" in log  # os.getenv works; the variable was dropped
    assert "PRISMTEST getenv=nil" in log
    assert "lua-secret-value" not in log
    assert "PRISMTEX-SECRET-LUA" not in log
    assert not (outside / "lua-written.txt").exists()
    assert not (store.layout.projects / "lua-escaped.txt").exists()
    for folder in (store.project_dir(pid), store.layout.out(pid)):
        assert not (folder / "lua-execute").exists()
        assert not (folder / "lua-popen").exists()


# -- timeout ---------------------------------------------------------------------


def _processes_mentioning(text: str) -> list[str]:
    """Command lines of running processes that contain `text`."""
    found: list[str] = []
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            except OSError:
                continue
            if text in cmdline:
                found.append(cmdline)
        return found
    if sys.platform == "win32":
        query = ["wmic", "process", "get", "CommandLine"]
    else:
        query = ["ps", "-A", "-o", "command="]
    listing = subprocess.run(query, capture_output=True, text=True, check=False).stdout
    return [line for line in listing.splitlines() if text in line and "wmic" not in line]


def test_an_infinite_loop_times_out_and_leaves_no_process(store: ProjectStore) -> None:
    pid = _project(store, {"main.tex": _article("\\def\\spin{\\spin}\\spin")})
    compiler = Compiler(store, timeout_s=3)
    started = time.monotonic()
    result = compiler.compile(pid)
    assert time.monotonic() - started < 30
    assert result.status == "timeout"
    assert [d.kind for d in result.diagnostics] == ["timeout"]
    assert result.diagnostics[0].message == "The compile was stopped after 3 seconds."
    out = str(store.layout.out(pid))
    deadline = time.monotonic() + 10
    while _processes_mentioning(out) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _processes_mentioning(out) == []


# -- diagnostics -------------------------------------------------------------------


def test_error_in_an_input_file_has_its_file_and_line(
    store: ProjectStore, compiler: Compiler
) -> None:
    pid = _project(
        store,
        {
            "main.tex": _article("Intro.\n\\input{sections/x}\nOutro."),
            "sections/x.tex": "First line.\nSecond line.\nThird \\undefinedmacro{} line.\n",
        },
    )
    result = compiler.compile(pid)
    assert result.status == "failed"
    first = result.diagnostics[0]
    assert (first.kind, first.severity, first.file, first.line) == (
        "undefined_control_sequence",
        "error",
        "sections/x.tex",
        3,
    )
    assert "\\undefinedmacro" in first.message


def test_undefined_citation(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(
        store,
        {
            "main.tex": _article(
                "See \\cite{nokey}.\n\\bibliographystyle{plain}\n\\bibliography{refs}"
            ),
            "refs.bib": "@misc{other, title = {Other}, year = {2020}}\n",
        },
    )
    result = compiler.compile(pid)
    citations = [d for d in result.diagnostics if d.kind == "undefined_citation"]
    assert [(d.file, d.line) for d in citations] == [("main.tex", 3)]
    assert "nokey" in citations[0].message
    assert any(d.kind == "bibtex_warning" and "nokey" in d.message for d in result.diagnostics)


def test_an_old_pdf_is_not_reported_for_a_broken_document(
    store: ProjectStore, compiler: Compiler
) -> None:
    info = store.create(kind="blank")
    assert compiler.compile(info.id).status == "ok"
    store.write(
        info.id, "main.tex", _article("Hi.").replace("\n", "\n\\usepackage{nosuchpkg}\n", 1), None
    )
    for _ in range(2):  # the second run is latexmk's unchanged re-run
        result = compiler.compile(info.id)
        assert result.status == "failed"
        assert result.pdf_path is None
        assert "missing_file" in _kinds(result)


def test_bibtex_errors_go_away_with_the_bibliography(
    store: ProjectStore, compiler: Compiler
) -> None:
    broken = "@misc{a,\n  title = {A}\n  year = {2020}\n}\n"
    pid = _project(
        store,
        {
            "main.tex": _article("\\cite{a}\n\\bibliographystyle{plain}\n\\bibliography{refs}"),
            "refs.bib": broken,
        },
    )
    assert "bibtex_error" in _kinds(compiler.compile(pid))
    store.write(pid, "main.tex", _article("No bibliography now."), None)
    result = compiler.compile(pid)
    assert result.status == "ok"
    assert "bibtex_error" not in _kinds(result)


# -- HTTP --------------------------------------------------------------------------


def test_compile_and_fetch_the_pdf_over_http(make_client: MakeClient, settings: Settings) -> None:
    client = make_client(settings=settings)
    pid = client.post("/api/projects", json={"kind": "paper"}).json()["id"]
    body = client.post(f"/api/projects/{pid}/compile").json()
    assert (body["status"], body["ok"]) == ("ok", True)
    assert body["pdf_url"].startswith(f"/api/projects/{pid}/pdf?v=")
    response = client.get(body["pdf_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    [meta] = client.get(f"/api/projects/{pid}/logs").json()["logs"]
    assert (meta["log_id"], meta["status"], meta["errors"]) == (body["log_id"], "ok", 0)
