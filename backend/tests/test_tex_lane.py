"""The TeX lane (`pytest -m tex`): the hardened compile against real TeX.

These tests fail, never skip, when latexmk (or, for its test, lualatex) is
missing: a lane that silently skips proves nothing.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prismtex.compile import MAX_LOG_TEXT_BYTES, Compiler, CompileResult, build_tex_env
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


def test_lualatex_is_refused_at_compile_time(store: ProjectStore, compiler: Compiler) -> None:
    # SECURITY-2: lualatex is disabled this release (its Lua layer reads
    # arbitrary files even under --safer; the .fls audit cannot see it).
    info = store.create(kind="blank")
    result = compiler.compile(info.id, engine="lualatex")
    assert result.status == "unavailable"
    assert result.engine == "lualatex"
    assert [d.kind for d in result.diagnostics] == ["engine_disabled"]
    assert result.pdf_path is None


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


# -- \include from a subfolder (SPEC-1) ----------------------------------------


def test_include_of_a_file_in_a_subfolder_compiles(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(
        store,
        {
            "main.tex": "\\documentclass{report}\n\\begin{document}\n"
            "\\include{chapters/one}\n\\end{document}\n",
            "chapters/one.tex": "Chapter one.\n",
        },
    )
    project = store.project_dir(pid)
    before = _snapshot(project)
    for _ in range(2):  # the second run exercises a rerun over the mirrored tree
        result = compiler.compile(pid)
        assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
        assert result.pdf_path is not None
        assert not [d for d in result.diagnostics if d.severity == "error"]
    assert _snapshot(project) == before


def test_include_with_a_root_file_in_a_subfolder(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(
        store,
        {
            "paper/main.tex": "\\documentclass{report}\n\\begin{document}\n"
            "\\include{paper/ch1}\n\\end{document}\n",
            "paper/ch1.tex": "Chapter one.\n",
        },
    )
    project = store.project_dir(pid)
    before = _snapshot(project)
    for _ in range(2):
        result = compiler.compile(pid, root_file="paper/main.tex")
        assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
        assert result.pdf_path == store.layout.out(pid) / "main.pdf"
    assert _snapshot(project) == before


# -- no missfont.log in the project folder (SPEC-2) ----------------------------


def test_missing_font_leaves_no_missfont_log(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(store, {"main.tex": _article("\\font\\x=prismtexnosuchfont \\x hello")})
    project = store.project_dir(pid)
    before = _snapshot(project)
    result = compiler.compile(pid)
    assert result.status == "failed"
    assert not (project / "missfont.log").exists()
    assert _snapshot(project) == before


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


# -- pdfTeX \pdfobj reads outside, caught by the .fls audit (SECURITY-1) --------


def _pdfobj(target: str) -> str:
    # pdfTeX copies the file's bytes into the PDF as an object stream; this
    # bypasses openin_any, so the .fls audit is what stops it.
    return (
        f"\\immediate\\pdfobj stream file {{{target}}}"
        "\\pdfcatalog{/PrismLeak \\the\\pdflastobj\\space 0 R}\nHello."
    )


def test_pdfobj_of_an_absolute_path_is_caught_and_the_pdf_discarded(
    store: ProjectStore, compiler: Compiler, outside: Path
) -> None:
    (outside / "secret.txt").write_text("PRISMTEX-SECRET-PDFOBJ\n", encoding="utf-8")
    pid = _project(store, {"main.tex": _article(_pdfobj((outside / "secret.txt").as_posix()))})
    result = compiler.compile(pid)
    assert result.status != "ok"
    assert result.pdf_path is None
    assert "access_denied" in _kinds(result)
    assert any(d.kind == "access_denied" and d.severity == "error" for d in result.diagnostics)
    out = store.layout.out(pid)
    assert not (out / "main.pdf").exists()
    # The secret reaches neither the stored log nor any surviving output file.
    assert "PRISMTEX-SECRET-PDFOBJ" not in result.log_text
    assert not any(
        b"PRISMTEX-SECRET-PDFOBJ" in p.read_bytes() for p in out.iterdir() if p.is_file()
    )


def test_pdfobj_of_a_parent_path_is_caught(store: ProjectStore, compiler: Compiler) -> None:
    pid = _project(store, {"main.tex": _article(_pdfobj("../pdfobj-neighbour.txt"))})
    neighbour = store.layout.projects / "pdfobj-neighbour.txt"
    neighbour.write_text("PRISMTEX-SECRET-NEIGHBOUR\n", encoding="utf-8")
    try:
        result = compiler.compile(pid)
    finally:
        neighbour.unlink()
    assert result.status != "ok"
    assert result.pdf_path is None
    assert "access_denied" in _kinds(result)
    assert "PRISMTEX-SECRET-NEIGHBOUR" not in result.log_text


def test_a_normal_pdfobj_of_a_project_file_still_compiles(
    store: ProjectStore, compiler: Compiler
) -> None:
    # A \pdfobj that reads a file inside the project is fine: the audit only
    # discards a PDF built from a read outside the project.
    pid = _project(
        store,
        {
            "main.tex": _article(_pdfobj("logo.txt")),
            "logo.txt": "an embedded project file\n",
        },
    )
    result = compiler.compile(pid)
    assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
    assert result.pdf_path is not None
    assert "access_denied" not in _kinds(result)


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


# A probe that shows --safer stubs io.open but not io.lines: the reason
# lualatex is refused rather than relied on (SECURITY-2). %SECRET% is filled in.
LUA_GAP_PROBE = r"""
\documentclass{article}
\begin{document}
\directlua{
  local f = io.open("%SECRET%","r")
  texio.write_nl("term and log","PRISMGAP io.open=" .. tostring(f))
  local ok = pcall(function()
    for line in io.lines("%SECRET%") do
      texio.write_nl("term and log","PRISMGAP io.lines=" .. line)
      break
    end
  end)
  texio.write_nl("term and log","PRISMGAP io.lines.ok=" .. tostring(ok))
}
Hello.
\end{document}
"""


def test_lualatex_is_refused_and_its_isolation_gap_is_real(
    store: ProjectStore, compiler: Compiler, outside: Path, tmp_path: Path
) -> None:
    assert shutil.which("lualatex"), "lualatex is not on PATH; this test needs it"

    # 1. Through the Compiler, lualatex is refused before it runs.
    pid = _project(store, {"main.tex": _article("Hello.")})
    refused = compiler.compile(pid, engine="lualatex")
    assert refused.status == "unavailable"
    assert [d.kind for d in refused.diagnostics] == ["engine_disabled"]

    # 2. Directly (bypassing the Compiler), prove the gap that justifies the
    #    refusal: --safer stubs io.open but leaves io.lines open, so the Lua
    #    layer can still read a file the hardening means to block. The .fls
    #    recorder does not even see this read, so the SECURITY-1 audit cannot
    #    catch it -- which is why lualatex must be refused outright.
    secret = outside / "secret.txt"
    secret.write_text("PRISMTEX-SECRET-LUA\n", encoding="utf-8")
    src = tmp_path / "lua-src"
    out = tmp_path / "lua-out"
    src.mkdir()
    out.mkdir()
    (src / "main.tex").write_text(LUA_GAP_PROBE.replace("%SECRET%", secret.as_posix()), "utf-8")
    env = build_tex_env({"PATH": os.environ.get("PATH", "")}, out)
    proc = subprocess.run(
        [
            "lualatex",
            "--safer",
            "--nosocket",
            "-interaction=nonstopmode",
            f"-output-directory={out}",
            "main.tex",
        ],
        cwd=src,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    log = (out / "main.log").read_text(encoding="utf-8", errors="replace") + proc.stdout
    assert 'safer option set, function "io.open" is disabled' in log
    assert 'safer option set, function "io.lines" is disabled' not in log
    assert "PRISMGAP io.open=nil" in log
    # On TeX Live 2023 io.lines reads the outside file outright. If a future
    # TeX Live closes it, this line changes and the refusal can be revisited.
    assert "PRISMGAP io.lines=PRISMTEX-SECRET-LUA" in log


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


def test_a_runaway_log_times_out_without_parsing_gigabytes(store: ProjectStore) -> None:
    # CORRECTNESS-3: a \message loop grows the .log without bound. On timeout
    # the log is neither parsed nor held in full, so the request returns near
    # the timeout, in bounded memory, with a bounded stored log.
    message = "A" * 4000
    pid = _project(
        store,
        {"main.tex": _article(f"\\def\\x{{\\message{{{message}}}\\x}}\\x")},
    )
    compiler = Compiler(store, timeout_s=3)
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()
    result = compiler.compile(pid)
    elapsed = time.monotonic() - started
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert result.status == "timeout"
    assert [d.kind for d in result.diagnostics] == ["timeout"]
    # Near the timeout, not ~3x it (the old code read and parsed the whole log).
    assert elapsed < 3 + 8, elapsed
    assert len(result.log_text.encode("utf-8")) <= MAX_LOG_TEXT_BYTES
    # The log on disk is far larger than what was read into memory.
    log_on_disk = store.layout.out(pid) / "main.log"
    assert log_on_disk.stat().st_size > 8 * 2**20
    assert (after_rss - before_rss) < 400 * 1024  # ru_maxrss is KiB on Linux


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


# -- biblatex + Biber, through the guard (Biber RCE / outside read) ------------

BIBLATEX_DOC = (
    "\\documentclass{{article}}\n"
    "\\usepackage[backend=biber]{{biblatex}}\n"
    "\\addbibresource{{{resource}}}\n"
    "\\begin{{document}}\n"
    "Cited \\cite{{knuth}}.\n"
    "\\printbibliography\n"
    "\\end{{document}}\n"
)
KNUTH = "@book{knuth, author={Knuth, Donald}, title={The TeXbook}, year={1984}, publisher={AW}}\n"


def test_biblatex_with_biber_produces_a_bibliography(
    store: ProjectStore, compiler: Compiler
) -> None:
    assert shutil.which("biber"), "biber is not on PATH; this test needs it"
    pid = _project(
        store,
        {"main.tex": BIBLATEX_DOC.format(resource="refs.bib"), "refs.bib": KNUTH},
    )
    result = compiler.compile(pid)
    assert result.status == "ok", [d.to_dict() for d in result.diagnostics]
    assert result.pdf_path is not None
    bbl = store.layout.out(pid) / "main.bbl"
    assert bbl.is_file()
    bbl_text = bbl.read_text(encoding="utf-8")
    assert "knuth" in bbl_text and "TeXbook" in bbl_text


def test_biber_refuses_an_absolute_addbibresource(
    store: ProjectStore, compiler: Compiler, outside: Path
) -> None:
    assert shutil.which("biber"), "biber is not on PATH; this test needs it"
    secret = outside / "secret.bib"
    secret.write_text(
        "@book{leak, author={Leak}, title={ABSOLUTE-SECRET-BIB}, year={1}, publisher={X}}\n",
        encoding="utf-8",
    )
    pid = _project(store, {"main.tex": BIBLATEX_DOC.format(resource=secret.as_posix())})
    result = compiler.compile(pid)
    # The guard refuses the absolute data source, so the entry never reaches
    # the .bbl or the PDF.
    assert result.status != "ok"
    bbl = store.layout.out(pid) / "main.bbl"
    assert "ABSOLUTE-SECRET-BIB" not in (bbl.read_text(encoding="utf-8") if bbl.is_file() else "")
    assert "ABSOLUTE-SECRET-BIB" not in result.log_text


def test_biber_does_not_execute_a_crafted_datatype(
    store: ProjectStore, compiler: Compiler, outside: Path
) -> None:
    # The eval-based RCE: a crafted `datatype` option becomes a Perl package
    # name biber would eval. The guard refuses the datasource before biber
    # runs, so no command executes.
    assert shutil.which("biber"), "biber is not on PATH; this test needs it"
    marker = outside / "biber-rce-marker"
    datatype = f"bibtex`touch {marker.as_posix()}`"
    doc = (
        "\\documentclass{article}\n"
        "\\usepackage[backend=biber]{biblatex}\n"
        f"\\addbibresource[datatype={{{datatype}}}]{{refs.bib}}\n"
        "\\begin{document}\n\\nocite{*}\n\\printbibliography\n\\end{document}\n"
    )
    pid = _project(store, {"main.tex": doc, "refs.bib": KNUTH})
    result = compiler.compile(pid)
    assert result.status != "ok"
    assert not marker.exists(), "the crafted datatype executed a command"


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
