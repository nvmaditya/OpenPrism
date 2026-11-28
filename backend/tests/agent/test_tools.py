"""The ToolBox: specs, argument checks, the escape and repeat guards (agent design §4.2)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import pytest

from prismtex.agent.context import PromptSet
from prismtex.agent.providers import ToolCall
from prismtex.agent.staging import Staging, capture_base
from prismtex.agent.tools import (
    AGENTIC_TOOLS,
    READ_ONLY_TOOLS,
    STOP_HINT,
    TRUNCATED_ARGS,
    ToolBox,
    ToolOutcome,
)
from prismtex.agent.verify import DiagnosticView, Verification
from prismtex.workspace import FileVersion, ProjectStore

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], FileVersion]
MakeToolbox = Callable[..., ToolBox]

BLANK = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
PAPER = (
    "\\documentclass{article}\n"  # 1
    "\\usepackage{amsmath}\n"  # 2
    "\\begin{document}\n"  # 3
    "\\section{Introduction}\\label{sec:intro}\n"  # 4
    "Graph networks learn from structured data.\n"  # 5
    "\n"  # 6
    "\\section{Method}\n"  # 7
    "We aggregate neighbour features.\n"  # 8
    "\\end{document}\n"  # 9
)


def call(
    name: str, raw: str, *, call_id: str = "c1", recovered: bool = False, cut: bool = False
) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        raw_args=raw,
        args=None,
        parse_error="truncated" if cut else None,
        recovered=recovered,
    )


def args(**values: object) -> str:
    return json.dumps(values)


def open_staging(project: Project, put: Put, files: dict[str, str]) -> Staging:
    store, pid = project
    for path, text in files.items():
        put(store, pid, path, text)
    staging, _ = Staging.open(capture_base(store, pid, None), None)
    return staging


def view_args(path: str = "main.tex") -> str:
    return args(path=path, start_line=None, end_line=None)


def view_id(out: ToolOutcome) -> str:
    found = re.search(r"view ([0-9a-f]{6})", out.for_model)
    assert found is not None, out.for_model
    return found.group(1)


@pytest.mark.anyio
async def test_single_backslash_latex_in_tool_args_is_rejected_with_the_lost_command_named(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    store, pid = blank_project
    put(store, pid, "main.tex", BLANK)
    staging, _ = Staging.open(capture_base(store, pid, None), None)
    box = make_toolbox(staging)
    raw = '{"path": "main.tex", "old_str": "Hello", "new_str": "Hello \\textbf{world}"}'  # \t: TAB
    out = await box.run(
        ToolCall(
            id="c1", name="str_replace", raw_args=raw, args=None, parse_error=None, recovered=False
        )
    )
    assert out.ok is False and out.error_code == "escape_damage" and out.changed == ()
    assert "TAB+'extbf'" in out.for_model and "\\textbf" in out.for_model
    assert out.for_model.endswith("Nothing was changed.")
    assert not staging.changed_since(0)


# -- the tool table ------------------------------------------------------------------------


def test_agentic_tools_are_eleven_sorted_specs_with_strict_schemas() -> None:
    names = [spec.name for spec in AGENTIC_TOOLS]
    assert names == sorted(names) and len(names) == 11
    assert names == [
        "add_bib_entry",
        "check",
        "create",
        "delete_file",
        "ensure_package",
        "insert",
        "insert_after_heading",
        "outline",
        "search",
        "str_replace",
        "view",
    ]
    assert [spec.name for spec in READ_ONLY_TOOLS] == ["outline", "search", "view"]
    for spec in AGENTIC_TOOLS:
        schema = spec.input_schema
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"]), spec.name
        assert spec.latex_fields <= set(schema["properties"]), spec.name
        assert not re.search(r"[\w-]+/[\w.-]+\.(?:tex|bib)", spec.description), spec.name
    by_name = {spec.name: spec for spec in AGENTIC_TOOLS}
    assert by_name["check"].input_schema["properties"] == {}
    assert by_name["str_replace"].latex_fields == {"old_str", "new_str"}
    assert by_name["add_bib_entry"].latex_fields == {"bibtex"}
    assert by_name["create"].latex_fields == by_name["insert"].latex_fields == {"text"}
    assert by_name["view"].input_schema["properties"]["start_line"]["type"] == ["integer", "null"]
    prompts = PromptSet.load()
    with_tools = prompts.version("agentic", AGENTIC_TOOLS)
    assert re.fullmatch(r"[0-9a-f]{8}", with_tools) and with_tools != prompts.version("agentic")


def test_specs_send_read_only_tools_on_question_turns_under_records_memory_only(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    question = make_toolbox(staging, intent="question")
    edit = make_toolbox(staging, intent="edit")
    assert question.specs("records") == READ_ONLY_TOOLS
    assert question.specs("transcript") == AGENTIC_TOOLS == edit.specs("transcript")
    assert edit.specs("records") == AGENTIC_TOOLS


# -- refusals, in order --------------------------------------------------------------------


@pytest.mark.anyio
async def test_tools_deny_project_state_build_scripts_and_escapes(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    store, pid = blank_project
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    store.write_bytes(pid, "latexmkrc", b"$pdf_mode = 1;\n")
    staging, _ = Staging.open(capture_base(store, pid, None), None)
    box = make_toolbox(staging)
    cases = [
        ("view", view_args(".prism/meta.json"), "path_denied"),
        ("create", args(path=".prism/meta.json", text="{}"), "path_denied"),
        ("create", args(path="latexmkrc", text="$pdf_mode = 0;\n"), "write_denied"),
        ("delete_file", args(path="latexmkrc"), "write_denied"),
        ("create", args(path="../x.tex", text="x\n"), "path_denied"),
        ("view", view_args("../x"), "path_denied"),
        ("create", args(path="main.tex:ads", text="x\n"), "path_denied"),
        ("view", view_args("main.tex:ads"), "path_denied"),
        ("create", args(path="CON.tex", text="x\n"), "path_denied"),
        ("create", args(path="/etc/x.tex", text="x\n"), "path_denied"),
        ("search", args(query="x", path=".git", regex=None), "path_denied"),
    ]
    for number, (name, raw, code) in enumerate(cases):
        out = await box.run(call(name, raw, call_id=f"c{number}"))
        assert (out.ok, out.error_code, out.changed) == (False, code, ()), (name, raw, out)
    assert staging.files() == [] and not staging.changed_since(0)


@pytest.mark.anyio
async def test_check_tool_accepts_no_arguments(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    runs: list[int] = []

    async def check() -> Verification:
        runs.append(1)
        return Verification("clean", None, 0, 0, 0, (), (), 0, (), True)

    box = make_toolbox(staging, check=check)
    refused = await box.run(call("check", '{"engine": "xelatex"}'))
    assert refused.error_code == "invalid_args" and "engine" in refused.for_model
    assert runs == []
    assert (await box.run(call("check", "{}"))).for_model == "Compiles with no new problems."
    assert (await box.run(call("check", ""))).ok  # no arguments at all reads as {}
    assert runs == [1, 1]


@pytest.mark.anyio
async def test_unknown_tools_cut_off_input_and_invalid_arguments_are_refused_with_reasons(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    unknown = await box.run(call("compile", "{}"))
    assert unknown.error_code == "unknown_tool" and "str_replace" in unknown.for_model
    cut = await box.run(call("str_replace", '{"path": "main.tex", "old_s', cut=True))
    assert (cut.error_code, cut.for_model) == ("truncated_args", TRUNCATED_ARGS)
    broken = await box.run(call("view", '{"path": "main.tex",'))
    assert broken.error_code == "invalid_args" and "not valid JSON at 20: " in broken.for_model
    missing = await box.run(call("insert", args(path="main.tex", after_line=1, text="x")))
    assert missing.error_code == "invalid_args" and "'view_id'" in missing.for_model
    assert missing.for_model.endswith("Nothing was changed.")
    typed = await box.run(call("view", args(path="main.tex", start_line="1", end_line=None)))
    assert typed.error_code == "invalid_args" and "start_line" in typed.for_model


@pytest.mark.anyio
async def test_question_turn_refuses_every_tool_but_outline_search_and_view(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging, intent="question")
    edit = await box.run(call("str_replace", args(path="main.tex", old_str="Hello", new_str="Hi")))
    assert edit.error_code == "read_only_turn" and "question" in edit.for_model
    assert (await box.run(call("check", "{}"))).error_code == "read_only_turn"
    package = await box.run(call("ensure_package", args(name="booktabs", options=None)))
    assert package.error_code == "read_only_turn"
    assert (await box.run(call("view", view_args()))).ok
    assert (await box.run(call("outline", "{}"))).ok
    assert (await box.run(call("search", args(query="hello", path=None, regex=None)))).ok
    assert not staging.changed_since(0)


# -- the escape guard ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_escape_guard_takes_line_breaks_as_written_when_the_call_escapes_backslashes(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    # JSON "\\begin{align}\nu(x) ..." is an escaped \begin, then a line starting with u.
    raw = (
        '{"path": "main.tex", "old_str": "Hello", '
        '"new_str": "\\\\begin{align}\\nu(x) &= 1 \\\\\\\\\\n\\top &= 2\\n\\\\end{align}"}'
    )
    out = await box.run(call("str_replace", raw))
    assert out.ok, out.for_model
    assert (
        "\\begin{align}\nu(x) &= 1 \\\\\n\top &= 2\n\\end{align}" in staging.read("main.tex").text
    )
    # The evidence holds for the whole call: old_str copies two lines with no backslash.
    raw = (
        '{"path": "main.tex", "old_str": "\\\\begin{align}\\nu(x) &= 1", '
        '"new_str": "\\\\begin{align}\\nu(x) &= \\\\alpha"}'
    )
    assert (await box.run(call("str_replace", raw))).ok
    two_lines = '{"path": "main.tex", "old_str": "&= 2\\n", "new_str": "&= 3\\nu &= 4\\n"}'
    alone = await box.run(call("str_replace", two_lines))
    assert alone.error_code == "escape_damage"  # this call shows no escaped backslash


@pytest.mark.anyio
async def test_escape_guard_without_escaped_backslashes_names_the_damage_and_a_way_out(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    lf = await box.run(
        call("str_replace", '{"path": "main.tex", "old_str": "Hello", "new_str": "Let\\nu = 0"}')
    )
    assert lf.error_code == "escape_damage"
    assert "new_str line 1 has LF+'u'" in lf.for_model and "(write \\\\nu)" in lf.for_model
    assert "start that line with a space" in lf.for_model
    tab = await box.run(
        call("str_replace", '{"path": "main.tex", "old_str": "Hello", "new_str": "a\\top"}')
    )
    assert tab.error_code == "escape_damage" and "TAB+'op'" in tab.for_model
    assert "indent with spaces" in tab.for_model
    spaced = await box.run(
        call("str_replace", '{"path": "main.tex", "old_str": "Hello", "new_str": "Let\\n u = 0"}')
    )
    assert spaced.ok and "Let\n u = 0\n" in staging.read("main.tex").text


@pytest.mark.anyio
async def test_escape_guard_stays_on_for_lenient_and_recovered_calls_and_control_characters(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    # \s is not a JSON escape, so the call parses only leniently: the model single-escapes.
    lenient = '{"path": "main.tex", "old_str": "Hello", "new_str": "\\section{A} $\\nu$"}'
    out = await box.run(call("str_replace", lenient))
    assert out.error_code == "escape_damage" and "LF+'u'" in out.for_model
    escaped = '{"path": "main.tex", "old_str": "Hello", "new_str": "\\\\begin{align}\\nu = 1"}'
    recovered = await box.run(call("str_replace", escaped, recovered=True))
    assert recovered.error_code == "escape_damage" and "LF+'u'" in recovered.for_model
    control = '{"path": "main.tex", "old_str": "Hello", "new_str": "\\\\alpha \\beta"}'
    out = await box.run(call("str_replace", control))
    assert out.error_code == "escape_damage"
    assert "BS+'eta'" in out.for_model and "into a backspace" in out.for_model
    assert not staging.changed_since(0)


@pytest.mark.anyio
async def test_escape_guard_reads_a_literal_backslash_n_as_code_only_beside_real_line_breaks(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    doubled = (
        '{"path": "main.tex", "old_str": "Hello", "new_str": "\\\\section{A}\\\\n\\\\label{a}"}'
    )
    out = await box.run(call("str_replace", doubled))
    assert out.error_code == "escape_damage" and "escaped twice" in out.for_model
    listing = (
        '{"path": "main.tex", "old_str": "Hello", '
        '"new_str": "\\\\begin{verbatim}\\nprintf(\\"hi\\\\n\\");\\n\\\\end{verbatim}"}'
    )
    out = await box.run(call("str_replace", listing))
    assert out.ok, out.for_model
    assert 'printf("hi\\n");' in staging.read("main.tex").text
    crlf = await box.run(
        call(
            "insert",
            args(
                path="main.tex",
                after_line=0,
                text="% a\r\n% b",
                view_id=staging.read("main.tex").view_id,
            ),
        )
    )
    assert crlf.ok and staging.read("main.tex").text.startswith("% a\n% b\n")


# -- the repeat guard ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_repeat_guard_stops_on_the_third_identical_failing_call(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    raw = args(path="main.tex", old_str="Goodbye", new_str="Hi")
    first = await box.run(call("str_replace", raw, call_id="c1"))
    assert first.error_code == "not_found" and STOP_HINT not in first.for_model
    other = await box.run(call("str_replace", args(path="main.tex", old_str="Bye", new_str="Hi")))
    assert STOP_HINT not in other.for_model  # a different call has its own count
    second = await box.run(call("str_replace", raw, call_id="c2"))
    assert second.error_code == "not_found" and second.for_model.endswith(STOP_HINT)
    assert not box.gave_up
    third = await box.run(call("str_replace", raw, call_id="c3"))
    assert (third.ok, third.error_code) == (False, "repeated") and box.gave_up
    assert third.for_model.endswith("Nothing was changed.")


# -- reading -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_view_shows_at_most_250_lines_and_search_50_hits_with_bib_files(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    body = "".join(f"line {n}\n" for n in range(1, 301))
    bib = "@article{kipf2017,\n  title = {Graph line networks},\n}\n"
    staging = open_staging(blank_project, put, {"main.tex": body, "refs.bib": bib})
    box = make_toolbox(staging)
    view = await box.run(call("view", view_args()))
    assert view.ok and view.for_model.startswith("main.tex · lines 1-250 of 300 · view ")
    assert "  250| line 250" in view.for_model and "line 251\n" not in view.for_model
    assert "View from line 251" in view.for_model and view.for_user == "Viewed main.tex L1-L250"
    tail = await box.run(call("view", args(path="main.tex", start_line=290, end_line=None)))
    assert "lines 290-300 of 300" in tail.for_model
    past = await box.run(call("view", args(path="main.tex", start_line=400, end_line=None)))
    assert past.error_code == "out_of_range"
    hits = await box.run(call("search", args(query="LINE", path=None, regex=None)))
    lines = hits.for_model.split("\n")
    assert len([ln for ln in lines if re.match(r"[\w.]+:\d+: ", ln)]) == 50
    assert "251 more hits not shown" in hits.for_model and hits.for_user.endswith("301 hits")
    found = await box.run(call("search", args(query=r"kipf\d+", path=None, regex=True)))
    assert found.for_model == "refs.bib:1: @article{kipf2017,"
    scoped = await box.run(call("search", args(query="kipf", path="main.tex", regex=False)))
    assert scoped.ok and scoped.for_model.startswith("No matches")
    bad = await box.run(call("search", args(query="(", path=None, regex=True)))
    assert bad.error_code == "invalid_args"


@pytest.mark.anyio
async def test_missing_path_suggests_close_staged_paths(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(
        blank_project, put, {"main.tex": BLANK, "sections/intro.tex": "Intro.\n"}
    )
    box = make_toolbox(staging)
    out = await box.run(call("view", view_args("sections/intro.txt")))
    assert out.error_code == "not_found" and "Did you mean: sections/intro.tex?" in out.for_model
    edit = await box.run(call("str_replace", args(path="mian.tex", old_str="a", new_str="b")))
    assert "Did you mean: main.tex?" in edit.for_model
    assert edit.for_model.endswith("Nothing was changed.")


@pytest.mark.anyio
async def test_outline_lists_numbered_headings_inputs_and_unincluded_files(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    root = PAPER.replace("\\end{document}", "\\input{sections/extra}\n\\end{document}")
    staging = open_staging(
        blank_project,
        put,
        {"main.tex": root, "sections/extra.tex": "\\section{Extra}\n", "notes.tex": "n\n"},
    )
    out = await make_toolbox(staging).run(call("outline", "{}"))
    assert "1. main.tex:L4-L5 \\section{Introduction} \\label{sec:intro}" in out.for_model
    assert "3. sections/extra.tex:L1-L1 \\section{Extra}" in out.for_model
    assert "main.tex includes sections/extra.tex" in out.for_model
    assert "Not included by the root file: notes.tex" in out.for_model


@pytest.mark.anyio
async def test_large_results_are_cut_to_4000_tokens_with_a_note(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    body = "".join(f"{n} " + "x" * 200 + "\n" for n in range(1, 241))
    staging = open_staging(blank_project, put, {"main.tex": body})
    out = await make_toolbox(staging).run(call("view", view_args()))
    assert out.ok and len(out.for_model) <= 4_000 * 3
    assert out.for_model.endswith("View a smaller range or narrow the search.]")


# -- editing -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_str_replace_miss_quotes_the_closest_lines_raw_and_numbered(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": PAPER})
    box = make_toolbox(staging)
    raw = args(path="main.tex", old_str="Graph network learn from structured data", new_str="x")
    out = await box.run(call("str_replace", raw))
    assert out.error_code == "not_found"
    assert out.for_model.startswith(
        "str_replace failed: old_str was not found in main.tex (9 lines).\n"
        "Closest match (similarity "
    )
    assert "at line 5:\n  5| Graph networks learn from structured data.\n" in out.for_model
    assert 'Copy old_str exactly from these lines, without the "NN| " prefixes.' in out.for_model
    assert out.for_model.endswith("Nothing was changed.") and "\\n" not in out.for_model
    twice = PAPER.replace("We aggregate", "Graph networks learn")
    staging = open_staging(blank_project, put, {"main.tex": twice})
    box = make_toolbox(staging)
    raw = args(path="main.tex", old_str="Graph networks learn", new_str="x")
    out = await box.run(call("str_replace", raw))
    assert out.error_code == "ambiguous"
    assert out.for_model.startswith("old_str matches 2 places (lines 5, 8). Include neighbouring")


@pytest.mark.anyio
async def test_str_replace_and_insert_report_the_new_lines_and_a_stale_view_is_refused(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": PAPER})
    box = make_toolbox(staging)
    first = view_id(await box.run(call("view", view_args())))
    raw = args(path="main.tex", old_str="  We aggregate neighbour features.  ", new_str="We sum.")
    edit = await box.run(call("str_replace", raw))
    assert edit.ok and edit.changed == ("main.tex",)
    assert "matched ignoring indentation at line 8" in edit.for_model
    assert "Line 8 now read:\n  8| We sum." in edit.for_model
    stale = await box.run(
        call("insert", args(path="main.tex", after_line=5, text="New.", view_id=first))
    )
    assert stale.error_code == "stale_view" and stale.changed == ()
    current = view_id(await box.run(call("view", view_args())))
    assert current == view_id(edit) != first
    raw = args(path="main.tex", old_str="We   sum.", new_str="We sum.")
    same = await box.run(call("str_replace", raw))  # matches only ignoring spaces: a no-op
    assert same.ok and same.changed == () and "already reads that way" in same.for_model
    assert view_id(await box.run(call("view", view_args()))) == current
    added = await box.run(
        call("insert", args(path="main.tex", after_line=5, text="New.", view_id=current))
    )
    assert added.ok and staging.read("main.tex").text.split("\n")[5] == "New."
    outside = await box.run(
        call("insert", args(path="main.tex", after_line=99, text="x", view_id=view_id(added)))
    )
    assert outside.error_code == "out_of_range" and "use 0 to 10" in outside.for_model


@pytest.mark.anyio
async def test_insert_after_heading_lands_in_the_section_body(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": PAPER})
    box = make_toolbox(staging)
    end = await box.run(
        call("insert_after_heading", args(heading="Introduction", text="End.", where=None))
    )
    assert end.ok
    lines = staging.read("main.tex").text.split("\n")
    assert lines[4:7] == ["Graph networks learn from structured data.", "End.", ""]
    start = await box.run(
        call("insert_after_heading", args(heading="sec:intro", text="Start.", where="start"))
    )
    assert start.ok and staging.read("main.tex").text.split("\n")[4] == "Start."
    missing = await box.run(
        call("insert_after_heading", args(heading="Results", text="x", where="end"))
    )
    assert missing.error_code == "not_found" and "1 Introduction (main.tex:4)" in missing.for_model


@pytest.mark.anyio
async def test_create_strips_fragment_wrappers_and_notes_a_file_the_root_never_includes(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    raw = args(path="sections/a.tex", text="\\begin{document}\nA.\n\\end{document}\n")
    out = await box.run(call("create", raw))
    assert out.ok and out.changed == ("sections/a.tex",)
    assert staging.read("sections/a.tex").text == "A.\n"
    assert "\\input{sections/a}" in out.for_model
    full = await box.run(call("create", args(path="other.tex", text=BLANK)))
    assert full.ok and staging.read("other.tex").text == BLANK
    exists = await box.run(call("create", args(path="main.tex", text="x\n")))
    assert exists.error_code == "exists"
    gone = await box.run(call("delete_file", args(path="other.tex")))
    assert gone.ok and gone.changed == ("other.tex",) and "other.tex" not in staging.paths()


@pytest.mark.anyio
async def test_plumbing_tools_write_as_host_and_are_idempotent(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    box = make_toolbox(staging)
    package = await box.run(call("ensure_package", args(name="booktabs", options=None)))
    assert package.ok and package.changed == ("main.tex",)
    assert staging.read("main.tex").text.split("\n")[1] == "\\usepackage{booktabs}"
    again = await box.run(call("ensure_package", args(name="booktabs", options="")))
    assert again.ok and again.changed == () and "loaded already" in again.for_model
    bad = await box.run(call("ensure_package", args(name="a b", options=None)))
    assert bad.error_code == "invalid_args"
    entry = "@article{kipf2017,\n  title = {Graph Convolutional Networks},\n  year = {2017}\n}"
    bib = await box.run(call("add_bib_entry", json.dumps({"bibtex": entry})))
    assert bib.ok and bib.changed == ("refs.bib", "main.tex"), bib.for_model
    assert "\\cite{kipf2017}" in bib.for_model
    assert "@article{kipf2017," in staging.read("refs.bib").text
    assert "\\bibliography{refs}" in staging.read("main.tex").text
    hosts = {f.path: f.host_lines for f in staging.files()}
    assert "\\usepackage{booktabs}" in hosts["main.tex"] and hosts["refs.bib"]
    same = await box.run(call("add_bib_entry", json.dumps({"bibtex": entry})))
    assert same.ok and same.changed == () and "already" in same.for_model
    clash = entry.replace("2017}", "2018}")
    taken = await box.run(call("add_bib_entry", json.dumps({"bibtex": clash})))
    assert taken.error_code == "exists"


@pytest.mark.anyio
async def test_check_reports_new_problems_with_numbered_lines_and_stops_after_three(
    blank_project: Project, put: Put, make_toolbox: MakeToolbox
) -> None:
    staging = open_staging(blank_project, put, {"main.tex": BLANK})
    problem = DiagnosticView(
        "main.tex", 3, "error", "undefined_control_sequence", "Undefined control\nsequence", None
    )

    async def check() -> Verification:
        return Verification(
            "regressed", None, 0, 0, 1, (problem,), (), 0, ("added \\usepackage{x}",), True
        )

    box = make_toolbox(staging, check=check)
    out = await box.run(call("check", "{}"))
    assert out.ok and out.for_user == "1 new problem"
    assert out.for_model == (
        "Your changes add 1 problem:\n"
        "main.tex:3: Undefined control sequence\n"
        "   1| \\documentclass{article}\n"
        "   2| \\begin{document}\n"
        " > 3| Hello\n"
        "   4| \\end{document}\n"
        "The host already added \\usepackage{x}."
    )
    await box.run(call("check", "{}"))
    await box.run(call("check", "{}"))
    limit = await box.run(call("check", "{}"))
    assert limit.error_code == "check_limit"
