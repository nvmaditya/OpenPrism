"""The TeX lane for the differential verifier (`pytest -m tex`).

These tests fail, never skip, when latexmk is missing.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable

import pytest

from prismtex.agent.staging import Staging, TurnBase, capture_base
from prismtex.agent.trace import TraceWriter
from prismtex.agent.verify import Verifier
from prismtex.cancel import CancelToken
from prismtex.compile import Compiler
from prismtex.compile.tree import ScratchTreeCompiler
from prismtex.workspace import FileVersion, ProjectStore

pytestmark = [pytest.mark.tex, pytest.mark.usefixtures("require_latexmk")]

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], FileVersion]
MakeTrace = Callable[..., TraceWriter]


@pytest.fixture
def require_latexmk() -> None:
    assert shutil.which("latexmk"), "latexmk is not on PATH; the TeX lane needs TeX Live or MiKTeX"


def article(body: str, preamble: str = "") -> str:
    return f"\\documentclass{{article}}\n{preamble}\\begin{{document}}\n{body}\\end{{document}}\n"


def tex(store: ProjectStore) -> ScratchTreeCompiler:
    return ScratchTreeCompiler(Compiler(store, timeout_s=120), store.layout)


def turn(store: ProjectStore, pid: str) -> tuple[TurnBase, Staging]:
    base = capture_base(store, pid, None)
    staging, _ = Staging.open(base, None)
    return base, staging


def edit(staging: Staging, path: str, old: str, new: str) -> None:
    text = staging.read(path).text
    assert old in text
    staging.write(path, text.replace(old, new, 1), by="model")


@pytest.mark.anyio
async def test_real_tex_new_undefined_macro_is_regressed_and_preexisting_one_is_not(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Intro.\n\\badmacro{x}\n"))
    compiler = tex(store)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "Intro.\nA new first paragraph.\n\n")
    verifier = Verifier(compiler, base, make_trace(pid), CancelToken())
    verifier.prime()

    moved = await verifier.check(staging, 0)
    assert (moved.verdict, moved.new, moved.fixed, moved.pdf) == ("clean", (), (), True), moved
    assert moved.baseline_errors == moved.errors == 1

    _, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "Intro. \\newbadmacro here.\n")
    added = await verifier.check(staging, 0)
    assert added.verdict == "regressed", added
    [new] = added.new
    assert (new.file, new.line, new.kind, new.message, new.excerpt) == (
        "main.tex",
        3,
        "undefined_control_sequence",
        "Undefined control sequence \\newbadmacro",
        "Intro. \\newbadmacro here.",
    )
    assert added.fixed == ()


@pytest.mark.anyio
async def test_masked_baseline_gives_unverified(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\input{nothere}\nIntro.\n"))
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "Intro, now with \\newbadmacro.\n")
    verifier = Verifier(tex(store), base, make_trace(pid), CancelToken())
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "masked"), v


@pytest.mark.anyio
async def test_real_tex_host_repair_adds_booktabs_for_toprule(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    base, staging = turn(store, pid)
    table = "\\begin{tabular}{l}\n\\toprule\nA\\\\\n\\bottomrule\n\\end{tabular}\n"
    edit(staging, "main.tex", "Hello\n", table)
    v = await Verifier(tex(store), base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.host_fixes) == ("clean", ("added \\usepackage{booktabs}",)), v
    assert "\\usepackage{booktabs}" in staging.read("main.tex").text


@pytest.mark.anyio
async def test_real_tex_host_repair_strips_document_wrappers_from_an_included_file(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\input{sections/a}\nAfter.\n"))
    put(store, pid, "sections/a.tex", "Old text.\n")
    base, staging = turn(store, pid)
    staging.write(
        "sections/a.tex",
        "\\documentclass{article}\n\\begin{document}\nNew text.\n\\end{document}\n",
        by="model",
    )
    v = await Verifier(tex(store), base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.verdict == "clean", v
    assert v.host_fixes == (
        "removed \\documentclass{article}, \\begin{document} and \\end{document}"
        " from sections/a.tex",
    )
    assert staging.read("sections/a.tex").text == "New text.\n"
