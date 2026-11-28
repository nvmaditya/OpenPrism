"""The differential verifier (agent design §2.2 `verify`, §7.1 to §7.5)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from prismtex.agent.proposals import LineMap
from prismtex.agent.staging import Staging, TurnBase, capture_base
from prismtex.agent.trace import TraceWriter
from prismtex.agent.verify import (
    DiagnosticDelta,
    DiagnosticView,
    Verifier,
    diff_diagnostics,
    fingerprint,
)
from prismtex.cancel import Cancelled, CancelToken
from prismtex.compile import CompileResult
from prismtex.compile.compiler import CompileStatus
from prismtex.compile.fake import FakeRule, FakeTreeCompiler, make_result
from prismtex.compile.logparse import Diagnostic
from prismtex.compile.tree import Purpose, SourceTree, source_tree
from prismtex.errors import RootFileInvalid
from prismtex.workspace import FileVersion, ProjectStore

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], FileVersion]
MakeTrace = Callable[..., TraceWriter]

UNDEFINED = "Undefined control sequence {match}"
BADMACRO = FakeRule(r"\\badmacro", "undefined_control_sequence", UNDEFINED)


def article(body: str, preamble: str = "") -> str:
    return f"\\documentclass{{article}}\n{preamble}\\begin{{document}}\n{body}\\end{{document}}\n"


def turn(store: ProjectStore, pid: str) -> tuple[TurnBase, Staging]:
    base = capture_base(store, pid, None)
    staging, _ = Staging.open(base, None)
    return base, staging


def edit(staging: Staging, path: str, old: str, new: str) -> None:
    text = staging.read(path).text
    assert old in text
    staging.write(path, text.replace(old, new, 1), by="model")


def events(trace: TraceWriter, kind: str, *, internal: bool = False) -> list[dict[str, Any]]:
    return [
        dict(env.data)
        for env in trace.reader().envelopes(internal=internal)
        if env.type == kind and env.internal == internal
    ]


def compiles(trace: TraceWriter) -> list[tuple[str, int, str]]:
    return [(e["stage"], e["round"], e["state"]) for e in events(trace, "compile")]


def purposes(compiler: FakeTreeCompiler) -> list[str]:
    return [purpose for _, purpose in compiler.calls]


class ScriptedCompiler:
    """A TreeCompiler whose compiles end with a fixed status per purpose."""

    def __init__(self, statuses: dict[str, CompileStatus]) -> None:
        self.statuses = statuses
        self.calls: list[str] = []

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        cancel.raise_if_set()
        self.calls.append(purpose)
        status = self.statuses[purpose]
        found: tuple[Diagnostic, ...] = ()
        if status == "timeout":
            found = (Diagnostic(None, None, "error", "timeout", "Stopped after 120 seconds."),)
        pdf = "x.pdf" if status == "ok" else None
        return make_result(status=status, diagnostics=found, pdf_path=pdf)

    def peek(self, digest: str) -> CompileResult | None:
        return None


class WithholdingCompiler(FakeTreeCompiler):
    """A fake that withholds everything after a read outside the project, as Phase 0 does."""

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        result = await super().compile(
            tree, purpose=purpose, cancel=cancel, timeout_s=timeout_s, turn_id=turn_id
        )
        if not any(d.kind == "access_denied" for d in result.diagnostics):
            return result
        message = "A file outside the project was read while building the PDF ('/etc/x')."
        denied = Diagnostic(None, None, "error", "access_denied", message)
        return replace(result, status="failed", diagnostics=(denied,), pdf_path=None)


class BlockingCompiler:
    """A TreeCompiler whose compile never finishes until its task is cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")

    def peek(self, digest: str) -> CompileResult | None:
        return None


class WithRealRootCheck(FakeTreeCompiler):
    """A fake that refuses a tree without its Root file, as the real Compiler does."""

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        if tree.root_file not in tree.inputs:
            raise RootFileInvalid("not_found", f"There is no file at '{tree.root_file}'.")
        return await super().compile(
            tree, purpose=purpose, cancel=cancel, timeout_s=timeout_s, turn_id=turn_id
        )


# -- a problem an edit only moved ------------------------------------------------------


@pytest.mark.anyio
async def test_preexisting_error_moved_by_an_edit_is_not_new(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(
        store,
        pid,
        "main.tex",
        "\\documentclass{article}\n\\begin{document}\nIntro.\n\\badmacro{x}\n\\end{document}\n",
    )
    compiler = FakeTreeCompiler(store, BADMACRO)
    base = capture_base(store, pid, None)
    staging, _ = Staging.open(base, None)
    text = staging.read("main.tex").text
    staging.write(
        "main.tex", text.replace("Intro.\n", "Intro.\nA new first paragraph.\n\n"), by="model"
    )
    verifier = Verifier(compiler, base, make_trace(pid), CancelToken())
    verifier.prime()
    v = await verifier.check(staging, 0)
    assert v.verdict == "clean" and v.reason is None
    assert (v.baseline_errors, v.errors, v.new, v.fixed) == (1, 1, (), ())
    assert [purpose for _, purpose in compiler.calls] == ["baseline", "proposal"]


# -- problems, excerpts and verdicts --------------------------------------------------


@pytest.mark.anyio
async def test_new_problem_is_reported_with_its_excerpt_errors_first(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Intro.\n"))
    citation = FakeRule(
        r"\\cite\{ghost\}",
        "undefined_citation",
        "Citation `ghost' on page 1 undefined",
        severity="warning",
    )
    compiler = FakeTreeCompiler(store, citation, BADMACRO)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "Intro.\nSee \\cite{ghost}.\n  \\badmacro{y}\t  here\n")
    trace = make_trace(pid)
    v = await Verifier(compiler, base, trace, CancelToken()).check(staging, 0)

    assert (v.verdict, v.reason, v.rounds) == ("regressed", None, 0)
    assert v.new == (
        DiagnosticView(
            "main.tex",
            5,
            "error",
            "undefined_control_sequence",
            "Undefined control sequence \\badmacro",
            "\\badmacro{y} here",
        ),
        DiagnosticView(
            "main.tex",
            4,
            "warning",
            "undefined_citation",
            "Citation `ghost' on page 1 undefined",
            "See \\cite{ghost}.",
        ),
    )
    assert (v.baseline_errors, v.errors, v.fixed, v.new_warnings, v.pdf) == (0, 1, (), 0, True)
    [diagnostics] = events(trace, "diagnostics")
    assert diagnostics == {
        "round": 0,
        "new": [d.to_dict() for d in v.new],
        "fixed": [],
        "persisting_errors": 0,
        "host_fixes": [],
    }


@pytest.mark.anyio
async def test_masked_baseline_fatal_before_the_first_change_gives_unverified_masked(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\input{nothere}\nIntro.\n"))
    fatal = FakeRule(r"\\input\{nothere\}", "fatal", "Emergency stop.", fatal=True, no_pdf=True)
    compiler = FakeTreeCompiler(store, fatal, BADMACRO)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "Intro, rewritten with \\badmacro.\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "masked")
    assert (v.new, v.fixed, v.host_fixes, v.pdf) == ((), (), (), False)


@pytest.mark.anyio
async def test_a_change_at_the_baseline_fatal_line_is_verified(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\input{nothere}\nIntro.\n"))
    fatal = FakeRule(r"\\input\{nothere\}", "fatal", "Emergency stop.", fatal=True, no_pdf=True)
    compiler = FakeTreeCompiler(store, fatal)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "\\input{nothere}\n", "")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.reason, v.new, v.pdf) == ("clean", None, (), True)
    assert [d.kind for d in v.fixed] == ["fatal"]


@pytest.mark.anyio
async def test_masking_follows_document_order_across_included_files(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\input{sections/a}\n\\input{nothere}\n"))
    put(store, pid, "sections/a.tex", "Old text.\n")
    put(store, pid, "z.tex", "Not included.\n")
    fatal = FakeRule(r"\\input\{nothere\}", "fatal", "Emergency stop.", fatal=True, no_pdf=True)
    compiler = FakeTreeCompiler(store, fatal)

    # sections/a.tex is read before the fatal line of main.tex: it was compiled.
    base, staging = turn(store, pid)
    edit(staging, "sections/a.tex", "Old text.", "New text.")
    verifier = Verifier(compiler, base, make_trace(pid), CancelToken())
    assert (await verifier.check(staging, 0)).verdict == "clean"

    # z.tex is outside the document, after its last line: never compiled.
    _, staging = turn(store, pid)
    edit(staging, "z.tex", "Not included.", "Changed.")
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "masked")


@pytest.mark.anyio
async def test_lost_pdf_is_a_new_problem(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Intro.\n"))
    nopdf = FakeRule(
        r"\\nopdf", "latex_warning", "No pages of output.", severity="warning", no_pdf=True
    )
    compiler = FakeTreeCompiler(store, nopdf)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "\\nopdf\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.verdict == "regressed"
    assert v.new == (DiagnosticView(None, None, "error", "no_pdf", "No PDF was produced.", None),)
    assert (v.errors, v.new_warnings, v.pdf) == (1, 1, False)


@pytest.mark.anyio
async def test_no_pdf_is_not_a_problem_when_the_baseline_made_none(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\nopdf\nIntro.\n"))
    nopdf = FakeRule(
        r"\\nopdf", "latex_warning", "No pages of output.", severity="warning", no_pdf=True
    )
    compiler = FakeTreeCompiler(store, nopdf)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Intro.\n", "Intro, again.\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.new, v.new_warnings, v.pdf) == ("clean", (), 0, False)


@pytest.mark.anyio
async def test_undefined_citation_warning_is_a_problem_and_other_warnings_are_counted_only(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Intro \\fontthing.\n"))
    rules = (
        FakeRule(
            r"\\cite\{ghost\}",
            "undefined_citation",
            "Citation `ghost' on page 1 undefined",
            severity="warning",
        ),
        FakeRule(r"\\fontthing", "font_warning", "Font shape not available", severity="warning"),
        FakeRule(r"\\wide", "overfull_box", "Overfull \\hbox (3.0pt too wide)", severity="info"),
    )
    compiler = FakeTreeCompiler(store, *rules)
    base, staging = turn(store, pid)
    edit(
        staging,
        "main.tex",
        "Intro \\fontthing.\n",
        "Intro \\fontthing.\n\\cite{ghost} \\fontthing \\wide\n",
    )
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.verdict == "regressed"
    assert [(d.kind, d.line) for d in v.new] == [("undefined_citation", 4)]
    assert (v.new_warnings, v.errors, v.baseline_errors) == (1, 0, 0)


@pytest.mark.anyio
async def test_undefined_reference_warning_that_the_turn_fixed_is_listed_as_fixed(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("See \\ref{sec:none}.\n"))
    reference = FakeRule(
        r"\\ref\{sec:none\}",
        "undefined_reference",
        "Reference `sec:none' on page 1 undefined",
        severity="warning",
    )
    compiler = FakeTreeCompiler(store, reference)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "\\ref{sec:none}", "\\ref{sec:intro}")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.new) == ("clean", ())
    assert [(d.kind, d.line) for d in v.fixed] == [("undefined_reference", 3)]


# -- host repairs ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_verifier_host_repair_adds_booktabs_for_toprule_with_one_extra_compile(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    toprule = FakeRule(
        r"\\toprule", "undefined_control_sequence", UNDEFINED, unless=r"\\usepackage\{booktabs\}"
    )
    compiler = FakeTreeCompiler(store, toprule)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello\n", "\\begin{tabular}{l}\\toprule\nA\\\\\n\\end{tabular}\n")
    trace = make_trace(pid)
    v = await Verifier(compiler, base, trace, CancelToken()).check(staging, 0)

    assert v.host_fixes == ("added \\usepackage{booktabs}",)
    assert (v.verdict, v.new, v.errors) == ("clean", (), 0)
    assert purposes(compiler) == ["baseline", "proposal", "proposal"]
    assert staging.read("main.tex").text.split("\n")[1] == "\\usepackage{booktabs}"
    [staged] = staging.files()
    assert staged.host_lines == ("\\usepackage{booktabs}",)

    assert compiles(trace) == [
        ("baseline", 0, "started"),
        ("baseline", 0, "finished"),
        ("proposal", 0, "started"),
        ("proposal", 0, "finished"),
        ("proposal", 0, "started"),
        ("proposal", 0, "finished"),
    ]
    first, second = events(trace, "diagnostics")
    assert ([d["kind"] for d in first["new"]], first["host_fixes"]) == (
        ["undefined_control_sequence"],
        [],
    )
    assert (second["new"], second["host_fixes"]) == ([], ["added \\usepackage{booktabs}"])
    [decision] = events(trace, "decision", internal=True)
    assert (decision["kind"], decision["choice"]) == ("host_repair", "added \\usepackage{booktabs}")


@pytest.mark.anyio
async def test_host_repair_adds_the_package_of_an_undefined_environment(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n", preamble="\\usepackage{amsmath}\n"))
    environment = FakeRule(
        r"\\begin\{tabularx\}",
        "undefined_environment",
        "Environment tabularx undefined",
        unless=r"\\usepackage\{tabularx\}",
    )
    compiler = FakeTreeCompiler(store, environment)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello\n", "\\begin{tabularx}{5cm}{X}\nA\n\\end{tabularx}\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.host_fixes) == ("clean", ("added \\usepackage{tabularx}",))
    assert staging.read("main.tex").text.split("\n")[1:3] == [
        "\\usepackage{amsmath}",
        "\\usepackage{tabularx}",
    ]


@pytest.mark.anyio
async def test_host_never_repairs_a_preexisting_problem(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\toprule\nHello\n"))
    toprule = FakeRule(r"\\toprule", "undefined_control_sequence", UNDEFINED)
    compiler = FakeTreeCompiler(store, toprule, BADMACRO)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello\n", "Hello \\badmacro\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.verdict == "regressed" and v.host_fixes == ()
    assert [d.message for d in v.new] == ["Undefined control sequence \\badmacro"]
    assert purposes(compiler) == ["baseline", "proposal"]


@pytest.mark.anyio
async def test_host_repair_strips_wrappers_from_the_turns_hunks_in_an_included_file(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\input{sections/a}\n"))
    put(store, pid, "sections/a.tex", "Old text.\n")
    preamble_only = FakeRule(
        r"\\begin\{document\}", "latex_error", "LaTeX Error: Can be used only in preamble."
    )
    compiler = FakeTreeCompiler(store, preamble_only)
    base, staging = turn(store, pid)
    staging.write(
        "sections/a.tex",
        "\\documentclass{article}\n\\begin{document}\nNew text.\n\\end{document}\n",
        by="model",
    )
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.host_fixes == (
        "removed \\documentclass{article}, \\begin{document} and \\end{document}"
        " from sections/a.tex",
    )
    assert (v.verdict, v.baseline_errors, v.errors) == ("clean", 1, 1)
    assert staging.read("sections/a.tex").text == "New text.\n"
    assert staging.read("main.tex").text == article("\\input{sections/a}\n")
    assert purposes(compiler) == ["baseline", "proposal", "proposal"]


CITE = FakeRule(
    r"\\cite\{kipf2017\}",
    "undefined_citation",
    "Citation `kipf2017' on page 1 undefined",
    severity="warning",
    unless=r"\\bibliography\{refs\}",
)
KIPF = "@article{kipf2017,\n  title = {Graph Convolutional Networks},\n  year = {2017}\n}\n"


@pytest.mark.anyio
async def test_host_repair_wires_a_bib_file_the_turn_added_the_cited_key_to(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello.\n"))
    compiler = FakeTreeCompiler(store, CITE)
    base, staging = turn(store, pid)
    staging.create("refs.bib", KIPF, by="model")
    edit(staging, "main.tex", "Hello.\n", "Hello \\cite{kipf2017}.\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.host_fixes == ("added \\bibliographystyle{plain} and \\bibliography{refs}",)
    assert (v.verdict, v.new) == ("clean", ())
    assert staging.read("main.tex").text == article(
        "Hello \\cite{kipf2017}.\n\\bibliographystyle{plain}\n\\bibliography{refs}\n"
    )


@pytest.mark.anyio
async def test_host_does_not_wire_a_bib_file_for_a_key_the_turn_did_not_add(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello.\n"))
    put(store, pid, "refs.bib", KIPF)
    compiler = FakeTreeCompiler(store, CITE)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello.\n", "Hello \\cite{kipf2017}.\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert (v.verdict, v.host_fixes) == ("regressed", ())
    assert purposes(compiler) == ["baseline", "proposal"]


# -- the security interaction ------------------------------------------------------------

DENIED = FakeRule(r"\\pdfobj", "access_denied", "read outside")


@pytest.mark.anyio
async def test_withheld_proposal_is_new_when_the_baseline_was_not_withheld(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    toprule = FakeRule(r"\\toprule", "undefined_control_sequence", UNDEFINED)
    compiler = WithholdingCompiler(store, DENIED, toprule)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello\n", "Hello \\toprule \\pdfobj\n")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.verdict == "regressed" and v.host_fixes == ()
    # The withheld PDF is lost too.
    assert [(d.kind, d.file) for d in v.new] == [("access_denied", None), ("no_pdf", None)]
    assert purposes(compiler) == ["baseline", "proposal"]


@pytest.mark.anyio
async def test_withheld_baseline_never_makes_the_turn_regressed(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("\\pdfobj\nHello\n"))
    compiler = WithholdingCompiler(store, DENIED, BADMACRO)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello\n", "Hello again\n")
    verifier = Verifier(compiler, base, make_trace(pid), CancelToken())
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason, v.new) == ("unverified", "masked", ())

    # The Turn removed the read: its problems may have been hidden in the Baseline.
    _, staging = turn(store, pid)
    edit(staging, "main.tex", "\\pdfobj\nHello\n", "\\badmacro\nHello\n")
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "masked")
    assert [d.kind for d in v.fixed] == ["access_denied"]

    _, staging = turn(store, pid)
    edit(staging, "main.tex", "\\pdfobj\nHello\n", "Hello\n")
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason, v.new) == ("clean", None, ())


# -- when the check cannot run ------------------------------------------------------------


@pytest.mark.anyio
async def test_tex_unavailable_and_timeout_give_unverified_with_a_reason(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello there")

    missing = FakeTreeCompiler(store, unavailable=True)
    v = await Verifier(missing, base, make_trace(pid, "t_a"), CancelToken()).check(staging, 0)
    assert (v.verdict, v.reason, v.new, v.pdf) == ("unverified", "tex_unavailable", (), False)
    assert purposes(missing) == ["baseline"]

    slow_baseline = ScriptedCompiler({"baseline": "timeout", "proposal": "ok"})
    trace = make_trace(pid, "t_b")
    v = await Verifier(slow_baseline, base, trace, CancelToken()).check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "timeout")
    assert slow_baseline.calls == ["baseline"]
    assert events(trace, "diagnostics") == []

    slow_proposal = ScriptedCompiler({"baseline": "ok", "proposal": "timeout"})
    trace = make_trace(pid, "t_c")
    v = await Verifier(slow_proposal, base, trace, CancelToken(), timeout_s=5).check(staging, 1)
    assert (v.verdict, v.reason, v.rounds, v.pdf) == ("unverified", "timeout", 1, False)
    assert slow_proposal.calls == ["baseline", "proposal"]
    assert compiles(trace)[-1] == ("proposal", 1, "finished")
    assert events(trace, "diagnostics") == []


@pytest.mark.anyio
async def test_project_without_a_root_file_is_unverified_no_root_file_and_never_compiles(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    store.update(pid, root_file=None)
    compiler = FakeTreeCompiler(store)
    base, staging = turn(store, pid)
    assert base.root_file is None
    edit(staging, "main.tex", "Hello", "Hello there")
    trace = make_trace(pid)
    verifier = Verifier(compiler, base, trace, CancelToken())
    verifier.prime()
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason, v.new, v.pdf) == ("unverified", "no_root_file", (), False)
    assert compiler.calls == []
    assert compiles(trace) == []


@pytest.mark.anyio
async def test_cancelled_turn_is_unverified_cancelled_without_a_compile(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    compiler = FakeTreeCompiler(store)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello there")
    cancel = CancelToken()
    cancel.set()
    v = await Verifier(compiler, base, make_trace(pid), cancel).check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "cancelled")
    assert compiler.calls == []


@pytest.mark.anyio
async def test_close_cancels_an_unfinished_baseline(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    compiler = BlockingCompiler()
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello there")
    verifier = Verifier(compiler, base, make_trace(pid), CancelToken())
    verifier.prime()
    verifier.prime()  # idempotent
    await compiler.started.wait()
    await verifier.close()
    assert compiler.cancelled
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "cancelled")


@pytest.mark.anyio
async def test_check_after_close_still_uses_a_finished_baseline(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    compiler = FakeTreeCompiler(store, BADMACRO)
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello \\badmacro")
    verifier = Verifier(compiler, base, make_trace(pid), CancelToken())
    verifier.prime()
    assert (await verifier.check(staging, 0)).verdict == "regressed"
    await verifier.close()
    await verifier.close()
    verifier.prime()
    v = await verifier.check(staging, 0)  # a verification at finalization
    assert v.verdict == "regressed"
    assert purposes(compiler) == ["baseline", "proposal"]


@pytest.mark.anyio
async def test_a_root_file_the_turn_deleted_is_a_new_problem(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    compiler = WithRealRootCheck(store)
    base, staging = turn(store, pid)
    staging.delete("main.tex", by="model")
    v = await Verifier(compiler, base, make_trace(pid), CancelToken()).check(staging, 0)
    assert v.verdict == "regressed"
    assert [d.kind for d in v.new] == ["root_file_invalid", "no_pdf"]


# -- caching and events ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_cached_baseline_sends_only_a_cached_compile_event(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello \\badmacro\n"))
    compiler = FakeTreeCompiler(store, BADMACRO)
    await compiler.compile(source_tree(store, pid), purpose="user", cancel=CancelToken())
    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello there")
    trace = make_trace(pid)
    verifier = Verifier(compiler, base, trace, CancelToken())
    verifier.prime()
    v = await verifier.check(staging, 0)
    assert (v.verdict, v.baseline_errors) == ("clean", 1)
    assert purposes(compiler) == ["user", "proposal"]
    [cached] = [e for e in events(trace, "compile") if e["stage"] == "baseline"]
    assert cached == {
        "stage": "baseline",
        "round": 0,
        "state": "cached",
        "ok": False,
        "errors": 1,
        "warnings": 0,
    }

    # The same Proposal tree again (a model check(), then the round's check): cached.
    await verifier.check(staging, 1)
    assert purposes(compiler) == ["user", "proposal"]
    assert compiles(trace) == [
        ("baseline", 0, "cached"),
        ("proposal", 0, "started"),
        ("proposal", 0, "finished"),
        ("proposal", 1, "cached"),
    ]
    assert [e["round"] for e in events(trace, "diagnostics")] == [0, 1]


@pytest.mark.anyio
async def test_compiles_pass_the_turn_id_and_the_timeout(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    seen: list[tuple[str, str | None, float]] = []

    class Recording(FakeTreeCompiler):
        async def compile(
            self,
            tree: SourceTree,
            *,
            purpose: Purpose,
            cancel: CancelToken,
            timeout_s: float = 120.0,
            turn_id: str | None = None,
        ) -> CompileResult:
            seen.append((purpose, turn_id, timeout_s))
            return await super().compile(
                tree, purpose=purpose, cancel=cancel, timeout_s=timeout_s, turn_id=turn_id
            )

    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello there")
    trace = make_trace(pid, "t_20261128T000000_abcd")
    await Verifier(Recording(store), base, trace, CancelToken(), timeout_s=7.5).check(staging, 0)
    assert seen == [
        ("baseline", "t_20261128T000000_abcd", 7.5),
        ("proposal", "t_20261128T000000_abcd", 7.5),
    ]


@pytest.mark.anyio
async def test_cancelled_compile_is_unverified_cancelled(
    blank_project: Project, put: Put, make_trace: MakeTrace
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", article("Hello\n"))
    cancel = CancelToken()

    class CancelOnProposal(FakeTreeCompiler):
        async def compile(
            self,
            tree: SourceTree,
            *,
            purpose: Purpose,
            cancel: CancelToken,
            timeout_s: float = 120.0,
            turn_id: str | None = None,
        ) -> CompileResult:
            if purpose == "proposal":
                cancel.set()
                raise Cancelled()
            return await super().compile(
                tree, purpose=purpose, cancel=cancel, timeout_s=timeout_s, turn_id=turn_id
            )

    base, staging = turn(store, pid)
    edit(staging, "main.tex", "Hello", "Hello there")
    trace = make_trace(pid)
    v = await Verifier(CancelOnProposal(store), base, trace, cancel).check(staging, 0)
    assert (v.verdict, v.reason) == ("unverified", "cancelled")
    assert compiles(trace)[-1] == ("proposal", 0, "started")


# -- pure functions ---------------------------------------------------------------------------


def test_fingerprint_masks_digits_and_ignores_line_numbers() -> None:
    a = DiagnosticView(
        "main.tex", 4, "error", "tex_error", "Missing $ inserted.\nl.4 $x_1", "$x_1 + y$"
    )
    moved = replace(a, line=40, message="Missing $ inserted.\nl.40 $x_1")
    assert fingerprint(a) == fingerprint(moved)
    assert fingerprint(a) != fingerprint(replace(a, excerpt="$x_2 + y$"))
    assert fingerprint(a) != fingerprint(replace(a, file="other.tex"))
    assert fingerprint(a) != fingerprint(replace(a, kind="latex_error"))
    assert fingerprint(a) != fingerprint(replace(a, excerpt=None))
    assert fingerprint(a) == fingerprint(replace(a, severity="warning"))
    assert len(fingerprint(a)) == 64


def view(line: int, excerpt: str, message: str = "Missing $ inserted.") -> DiagnosticView:
    return DiagnosticView("main.tex", line, "error", "tex_error", message, excerpt)


def test_diff_matches_a_problem_whose_line_moved_by_at_most_two_lines() -> None:
    # Base lines 1-4 are unchanged, base line 5 became new lines 5 and 6.
    maps = {"main.tex": LineMap("main.tex", ((1, 4, 0), (6, 10, 1)))}
    near = diff_diagnostics([view(3, "a")], [view(5, "b")], maps)
    assert near == DiagnosticDelta(new=(), fixed=(), persisting=(view(5, "b"),))
    far = diff_diagnostics([view(3, "a")], [view(6, "b")], maps)
    assert far == DiagnosticDelta(new=(view(6, "b"),), fixed=(view(3, "a"),), persisting=())
    # Base line 8 is new line 9; without a map it would be compared as line 8.
    shifted = diff_diagnostics([view(8, "a")], [view(11, "b")], maps)
    assert shifted.persisting == (view(11, "b"),)
    assert diff_diagnostics([view(8, "a")], [view(11, "b")], {}).new == (view(11, "b"),)


def test_diff_matches_a_problem_inside_a_replaced_hunk() -> None:
    # Base lines 5 to 9 were replaced by new lines 5 to 14.
    maps = {"main.tex": LineMap("main.tex", ((1, 4, 0), (10, 20, 5)))}
    delta = diff_diagnostics([view(5, "old")], [view(13, "new")], maps)
    assert (delta.new, delta.fixed) == ((), ())
    # The masked message must agree.
    other = view(13, "new", message="Extra }, or forgotten $.")
    delta = diff_diagnostics([view(5, "old")], [other], maps)
    assert (delta.new, delta.fixed) == ((other,), (view(5, "old"),))
    # A Baseline line outside the Hunk does not match by this rule.
    delta = diff_diagnostics([view(2, "old")], [view(13, "new")], maps)
    assert delta.new == (view(13, "new"),)


def test_diff_is_a_multiset_match_and_orders_new_errors_first() -> None:
    warning = DiagnosticView(
        "main.tex", 3, "warning", "undefined_citation", "Citation `x' undefined", "\\cite{x}"
    )
    same = view(7, "same")
    delta = diff_diagnostics([same], [warning, same, view(30, "same")], {})
    assert delta.persisting == (same,)
    assert delta.new == (view(30, "same"), warning)
    assert delta.fixed == ()
