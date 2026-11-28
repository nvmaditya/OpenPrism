"""The TeX lane for scratch tree compiles (`pytest -m tex`).

These tests fail, never skip, when latexmk is missing.
"""

from __future__ import annotations

import shutil
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from prismtex.cancel import CancelToken
from prismtex.compile import Compiler, CompileResult
from prismtex.compile.tree import ScratchTreeCompiler, source_tree
from prismtex.workspace import ProjectStore

pytestmark = [pytest.mark.tex, pytest.mark.usefixtures("require_latexmk")]

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], Any]

ARTICLE = "\\documentclass{{article}}\n{preamble}\\begin{{document}}\n{body}\n\\end{{document}}\n"


@pytest.fixture
def require_latexmk() -> None:
    assert shutil.which("latexmk"), "latexmk is not on PATH; the TeX lane needs TeX Live or MiKTeX"


class CountingDirs(Compiler):
    """The real Compiler, counting its scratch (compile_dir) runs."""

    def __init__(self, store: ProjectStore) -> None:
        super().__init__(store, timeout_s=120)
        self.dirs: list[Path] = []

    def compile_dir(self, src_dir: Path, **kwargs: Any) -> CompileResult:
        self.dirs.append(Path(src_dir))
        return super().compile_dir(src_dir, **kwargs)


def _png() -> bytes:
    """A valid 1x1 grey PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x80")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(folder).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _article(body: str, preamble: str = "") -> str:
    return ARTICLE.format(preamble=preamble, body=body)


@pytest.mark.anyio
async def test_scratch_compile_never_writes_the_project_folder(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    store.write_bytes(pid, "fig.png", _png())
    put(store, pid, "sections/intro.tex", "Intro text.\n")
    put(
        store,
        pid,
        "main.tex",
        _article(
            "\\input{sections/intro}\n"
            "\\newwrite\\notes\\immediate\\openout\\notes=notes.txt\n"
            "\\immediate\\write\\notes{written by TeX}\\immediate\\closeout\\notes\n"
            "\\includegraphics[width=1cm]{fig.png}\n",
            preamble="\\usepackage{graphicx}\n",
        ),
    )
    project = store.layout.project_dir(pid)
    before = _snapshot(project)
    tc = ScratchTreeCompiler(Compiler(store, timeout_s=120), store.layout)
    text = store.read(pid, "main.tex").text
    tree = source_tree(store, pid, overlay={"main.tex": text + "% staged\n"})
    result = await tc.compile(tree, purpose="baseline", cancel=CancelToken(), turn_id="t_tex")
    assert result.status == "ok", result.diagnostics
    assert _snapshot(project) == before
    assert result.pdf_path is not None and result.pdf_path.is_file()
    assert result.pdf_path.is_relative_to(store.layout.build(pid) / "scratch" / "t_tex")
    tc.cleanup(pid, "t_tex")
    assert not (store.layout.build(pid) / "scratch" / "t_tex").exists()
    assert _snapshot(project) == before


@pytest.mark.anyio
async def test_baseline_cache_hit_after_a_user_compile_of_the_same_tree(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", _article("Hello."))
    compiler = CountingDirs(store)
    tc = ScratchTreeCompiler(compiler, store.layout)
    user = await tc.compile(source_tree(store, pid), purpose="user", cancel=CancelToken())
    assert user.status == "ok", user.diagnostics
    assert user.pdf_path is not None and user.pdf_path.is_relative_to(store.layout.out(pid))
    runs = len(compiler.dirs)  # Compiler.compile runs compile_dir once, on the project folder
    text = store.read(pid, "main.tex").text
    baseline_tree = source_tree(store, pid, overlay={"main.tex": text})
    assert tc.peek(baseline_tree.digest) is user
    baseline = await tc.compile(
        baseline_tree, purpose="baseline", cancel=CancelToken(), turn_id="t_tex"
    )
    assert baseline is user and len(compiler.dirs) == runs


@pytest.mark.anyio
async def test_latexmk_bibtex_scratch_compile_resolves_citations(
    blank_project: Project, put: Put
) -> None:
    store, pid = blank_project
    bib = "@book{knuth1984,\n  author = {Donald Knuth},\n  title = {The TeXbook},\n"
    bib += "  publisher = {Addison-Wesley},\n  year = {1984}\n}\n"
    put(store, pid, "refs.bib", bib)
    body = "See \\cite{knuth1984}.\n\\bibliographystyle{plain}\n\\bibliography{refs}"
    tc = ScratchTreeCompiler(Compiler(store, timeout_s=120), store.layout)
    tree = source_tree(store, pid, overlay={"main.tex": _article(body)})
    result = await tc.compile(tree, purpose="proposal", cancel=CancelToken(), turn_id="t_bib")
    assert result.status == "ok", result.diagnostics
    kinds = {d.kind for d in result.diagnostics}
    assert not kinds & {"undefined_citation", "bibtex_error", "access_denied"}, result.diagnostics
    assert result.pdf_path is not None and result.pdf_path.is_file()


@pytest.mark.anyio
async def test_scratch_diagnostics_point_at_project_paths(blank_project: Project, put: Put) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", _article("\\input{sections/a}"))
    put(store, pid, "sections/a.tex", "Fine.\n")
    tc = ScratchTreeCompiler(Compiler(store, timeout_s=120), store.layout)
    tree = source_tree(store, pid, overlay={"sections/a.tex": "Fine.\nHere is \\undefinedthing.\n"})
    result = await tc.compile(tree, purpose="proposal", cancel=CancelToken(), turn_id="t_diag")
    assert result.status == "failed"
    located = [(d.kind, d.file, d.line, d.message) for d in result.diagnostics]
    assert (
        "undefined_control_sequence",
        "sections/a.tex",
        2,
        "Undefined control sequence \\undefinedthing",
    ) in located


def test_toolchain_names_latexmk_and_the_engines(store: ProjectStore) -> None:
    toolchain = Compiler(store).toolchain
    assert toolchain.startswith("Latexmk"), toolchain
    assert "pdfTeX" in toolchain, toolchain
