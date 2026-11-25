"""Log parser tables over real TeX, BibTeX, Biber and latexmk output.

The fixtures in fixtures/texlogs/ were produced on 2026-11-25 by TeX Live
2023 (pdfTeX 1.40.25, LuaHBTeX 1.17.0, BibTeX 0.99d, Biber 2.19, latexmk
4.83) running the hardened compile command on small documents written for
each case. Only the absolute build-folder paths were rewritten, to
/data/build/<case>/out. bib_missing_db.blg and bib_no_bibdata.blg come from
running bibtex by hand, because latexmk skips BibTeX in those cases.
lua_err and spaces_ok were compiled with lualatex; this machine lacks
luaotfload, hence their font warnings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prismtex.compile import (
    MAX_DIAGNOSTICS,
    Diagnostic,
    combine_diagnostics,
    parse_blg,
    parse_log,
    parse_tool_output,
)

FIXTURES = Path(__file__).parent / "fixtures" / "texlogs"

# (severity, kind, file, line, a fragment of the message)
Expected = tuple[str, str, str | None, int | None, str]

LOG_CASES: dict[str, list[Expected]] = {
    # Clean: the paper template compiles with no diagnostics at all.
    "paper_ok": [],
    # sections/x.tex line 3 has \foo, line 4 \bar in text; main.tex line 9
    # has a bad key and a missing figure; line 11 opens \textbf{ and never
    # closes it. Context lines echo an unbalanced "(" that must not confuse
    # the file stack: the warning on line 9 still belongs to main.tex.
    "errors": [
        ("error", "undefined_control_sequence", "sections/x.tex", 3, "\nl.3 Here is \\foo"),
        ("error", "tex_error", "sections/x.tex", 4, "Missing $ inserted."),
        ("error", "tex_error", "main.tex", 6, "Missing $ inserted."),
        ("error", "package_error", "main.tex", 9, "Package keyval Error: nosuchkey undefined."),
        ("warning", "latex_warning", "main.tex", 9, "File `figures/nothere.png' not found"),
        ("error", "missing_file", "main.tex", 9, "Package pdftex.def Error: File `figures/"),
        ("error", "tex_error", None, None, "File ended while scanning use of \\textbf"),
        ("error", "fatal", None, None, "Emergency stop."),
        ("error", "fatal", None, None, "job aborted, no legal \\end found"),
        ("error", "fatal", None, None, "==> Fatal error occurred, no output PDF file produced!"),
    ],
    # \begin{itemize} closed by \end{enumerate}, a lonely \item, a runaway
    # argument whose echoed text has an unbalanced "(", and an overfull box
    # whose displayed contents start with "(".
    "latex_errors": [
        ("warning", "latex_warning", "main.tex", None, "Unused global option(s): [nosuchoption]."),
        ("error", "latex_error", "main.tex", 4, "Something's wrong--perhaps a missing \\item."),
        ("error", "latex_error", "main.tex", 4, "\\begin{itemize} on input line 3 ended by"),
        ("error", "latex_error", "main.tex", 5, "Lonely \\item--perhaps a missing list"),
        ("info", "bad_box", "sections/boxes.tex", 1, "Overfull \\hbox (170.6865pt too wide)"),
        ("warning", "undefined_reference", "sections/boxes.tex", 3, "Reference `box:after'"),
        ("error", "tex_error", None, None, "File ended while scanning use of \\textbf"),
        ("error", "fatal", None, None, "Emergency stop."),
        ("error", "fatal", None, None, "job aborted, no legal \\end found"),
        ("error", "fatal", None, None, "==> Fatal error occurred"),
    ],
    # \usepackage{doesnotexist}: the "not found" message has no context of
    # its own and takes its line from the Emergency stop that follows.
    "missing_package": [
        ("error", "missing_file", "main.tex", 3, "File `doesnotexist.sty' not found."),
        ("error", "fatal", "main.tex", 3, "Emergency stop."),
        ("error", "fatal", "main.tex", 3, "==> Fatal error occurred"),
    ],
    "missing_input": [
        ("error", "missing_file", "main.tex", 4, "File `sections/nope.tex' not found."),
        ("error", "fatal", "main.tex", 4, "Emergency stop."),
        ("error", "fatal", "main.tex", 4, "==> Fatal error occurred"),
    ],
    "no_end": [
        ("error", "fatal", None, None, "Emergency stop."),
        ("error", "fatal", None, None, "job aborted, no legal \\end found"),
        ("error", "fatal", None, None, "==> Fatal error occurred"),
    ],
    # hyperref, a duplicate label (reported while reading main.aux, which is
    # not a project file), an undefined \ref and \cite, a font substitution,
    # and boxes in sections/boxes.tex. hyperref's warning appears twice in
    # the log and once here.
    "warnings": [
        ("warning", "multiply_defined_label", None, None, "Label `sec:a' multiply defined."),
        ("warning", "package_warning", "main.tex", 4, "(Unicode): removing `math shift'"),
        ("warning", "undefined_reference", "main.tex", 6, "Reference `sec:none' on page 1"),
        ("warning", "undefined_citation", "main.tex", 6, "Citation `nokey' on page 1 undefined"),
        ("warning", "font_warning", "main.tex", 7, "undefined using `OT1/cmr/bx/n' instead"),
        ("info", "bad_box", "sections/boxes.tex", 1, "Overfull \\hbox (170.99208pt too wide)"),
        ("info", "bad_box", "sections/boxes.tex", 4, "Underfull \\hbox (badness 10000)"),
        ("warning", "undefined_reference", "sections/boxes.tex", 5, "`sec:alsomissing'"),
        ("warning", "font_warning", "main.tex", None, "Some font shapes were not available"),
        ("warning", "latex_warning", "main.tex", None, "There were undefined references."),
        ("warning", "latex_warning", "main.tex", None, "There were multiply-defined labels."),
    ],
    "natbib": [
        (
            "warning",
            "undefined_citation",
            "main.tex",
            4,
            "Package natbib Warning: Citation `nokey'",
        ),
        ("warning", "package_warning", "main.tex", None, "There were undefined citations."),
    ],
    # biblatex quotes keys with straight quotes and continues its own
    # warnings with "(biblatex)" lines.
    "biblatex": [
        ("warning", "package_warning", None, None, "could not be found in the database: nokey"),
        ("warning", "undefined_citation", "main.tex", 5, "Citation 'nokey' on page 1 undefined"),
    ],
    # main.tex inputs sections/a.tex, which inputs sections/b.tex, then
    # "sections/my file.tex", whose name TeX prints unquoted.
    "nested": [
        ("error", "undefined_control_sequence", "sections/b.tex", 3, "l.3 Line three of b"),
        ("warning", "undefined_citation", "sections/a.tex", 3, "Citation `ghost'"),
        ("warning", "undefined_reference", "main.tex", 6, "Reference `sec:nowhere'"),
        ("error", "undefined_control_sequence", "sections/my file.tex", 2, "\\alsoundefined"),
        ("info", "bad_box", "main.tex", 12, "Overfull \\hbox (116.92813pt too wide)"),
        ("info", "bad_box", "main.tex", 12, "Underfull \\hbox (badness 10000)"),
        ("warning", "latex_warning", "main.tex", None, "There were undefined references."),
        ("warning", "latex_warning", "main.tex", None, "Label(s) may have changed."),
    ],
    "spaces_ok": [
        ("warning", "font_warning", None, None, "Font shape `OT1/lmr/m/n' undefined"),
        ("warning", "font_warning", "sections/my file.tex", 1, "Font shape `OT1/lmr/bx/n'"),
        ("warning", "undefined_reference", "sections/my file.tex", 1, "Reference `x' on page 1"),
        ("warning", "font_warning", "main.tex", None, "Some font shapes were not available"),
        ("warning", "latex_warning", "main.tex", None, "There were undefined references."),
    ],
    "lua_err": [
        ("warning", "font_warning", None, None, "Font shape `OT1/lmr/m/n' undefined"),
        ("error", "undefined_control_sequence", "main.tex", 3, "l.3 Hello \\undefinedmacro"),
        ("warning", "font_warning", "main.tex", None, "Some font shapes were not available"),
    ],
    # The hardening at work: each attempt fails inside TeX.
    "attack_input_abs": [
        ("error", "missing_file", "main.tex", 3, "File `/etc/hostname.tex' not found."),
        ("error", "fatal", "main.tex", 3, "Emergency stop."),
        ("error", "fatal", "main.tex", 3, "==> Fatal error occurred"),
    ],
    "attack_input_parent": [
        ("error", "missing_file", "main.tex", 3, "File `../outside.tex' not found."),
        ("error", "fatal", "main.tex", 3, "Emergency stop."),
        ("error", "fatal", "main.tex", 3, "==> Fatal error occurred"),
    ],
    "attack_openout": [
        ("error", "tex_error", "main.tex", 4, "I can't write on file `/tmp/prismtex-pwned.txt'."),
        ("error", "fatal", "main.tex", 4, "Emergency stop."),
        ("error", "fatal", "main.tex", None, "job aborted, file error in nonstop mode"),
        ("error", "fatal", "main.tex", 4, "==> Fatal error occurred"),
    ],
    # \write18 only logs "runsystem(touch pwned)...disabled.", which is not
    # a diagnostic.
    "attack_write18": [],
}

BLG_CASES: dict[str, list[Expected]] = {
    "paper_ok": [],
    "warnings": [
        ("warning", "bibtex_warning", None, None, 'I didn\'t find a database entry for "nokey"'),
    ],
    "natbib": [
        ("warning", "bibtex_warning", None, None, 'database entry for "nokey"'),
    ],
    # A location in main.aux is in a build file, so it is dropped.
    "bib_missing_db": [
        ("error", "bibtex_error", None, None, "I couldn't open database file nonexistent.bib"),
        ("error", "bibtex_error", None, None, "I found no database files"),
        ("warning", "bibtex_warning", None, None, 'database entry for "knuth1984"'),
    ],
    "bib_no_bibdata": [
        ("error", "bibtex_error", None, None, "I found no \\bibdata command"),
        ("warning", "bibtex_warning", None, None, 'database entry for "knuth1984"'),
    ],
    "bib_syntax": [
        ("error", "bibtex_error", "references.bib", 10, "I was expecting a `,' or a `}'"),
        ("warning", "bibtex_warning", None, None, "empty title in broken"),
        ("warning", "bibtex_warning", None, None, "empty journal in broken"),
        ("warning", "bibtex_warning", None, None, "empty year in broken"),
    ],
    # Warnings followed by a "--line N of file F" location.
    "bib_extra_field": [
        ("warning", "bibtex_warning", "references.bib", 4, 'ignoring knuth1984\'s extra "title"'),
        ("warning", "bibtex_warning", "references.bib", 8, 'entry type for "oddtype"'),
        ("warning", "bibtex_warning", None, None, "to sort, need author or key in oddtype"),
    ],
    # Biber writes a different format.
    "biblatex": [
        ("warning", "bibtex_warning", None, None, "I didn't find a database entry for 'nokey'"),
    ],
}

TOOL_CASES: dict[str, list[Expected]] = {
    "attack_input_abs": [
        (
            "warning",
            "access_denied",
            None,
            None,
            "pdflatex was not allowed to read '/etc/hostname'",
        ),
    ],
    "attack_openout": [
        ("warning", "access_denied", None, None, "not allowed to write '/tmp/prismtex-pwned.txt'"),
    ],
}

SPEC_KINDS = {
    "undefined_control_sequence",
    "missing_file",
    "latex_error",
    "package_error",
    "tex_error",
    "fatal",
    "undefined_citation",
    "undefined_reference",
    "multiply_defined_label",
    "font_warning",
    "package_warning",
    "latex_warning",
    "bad_box",
    "bibtex_error",
    "bibtex_warning",
}


def _check(found: list[Diagnostic], expected: list[Expected]) -> None:
    assert [(d.severity, d.kind, d.file, d.line) for d in found] == [e[:4] for e in expected]
    for diagnostic, (*_, fragment) in zip(found, expected, strict=True):
        assert fragment in diagnostic.message, (diagnostic, fragment)


def _src(case: str) -> Path:
    return Path(f"/data/projects/{case}")


@pytest.mark.parametrize(("case", "expected"), LOG_CASES.items(), ids=list(LOG_CASES))
def test_parse_log_fixture(case: str, expected: list[Expected]) -> None:
    text = (FIXTURES / f"{case}.log.txt").read_text(encoding="utf-8")
    _check(parse_log(text, src_dir=_src(case)), expected)


@pytest.mark.parametrize(("case", "expected"), BLG_CASES.items(), ids=list(BLG_CASES))
def test_parse_blg_fixture(case: str, expected: list[Expected]) -> None:
    text = (FIXTURES / f"{case}.blg").read_text(encoding="utf-8")
    _check(parse_blg(text, src_dir=_src(case)), expected)


@pytest.mark.parametrize(("case", "expected"), TOOL_CASES.items(), ids=list(TOOL_CASES))
def test_parse_tool_output_fixture(case: str, expected: list[Expected]) -> None:
    text = (FIXTURES / f"{case}.latexmk.txt").read_text(encoding="utf-8")
    _check(parse_tool_output(text), expected)


def test_fixtures_cover_every_kind_of_the_spec() -> None:
    covered = {e[1] for cases in (LOG_CASES, BLG_CASES) for exp in cases.values() for e in exp}
    assert SPEC_KINDS <= covered


def test_crlf_logs_parse_like_lf() -> None:
    text = (FIXTURES / "errors.log.txt").read_text(encoding="utf-8")
    lf = parse_log(text, src_dir=_src("errors"))
    assert parse_log(text.replace("\n", "\r\n"), src_dir=_src("errors")) == lf


def test_tool_output_without_denials_has_no_diagnostics() -> None:
    assert parse_tool_output("Latexmk: All targets (main.pdf) are up-to-date\n") == []


# -- paths ---------------------------------------------------------------------

_UNDEFINED = "{path}:3: Undefined control sequence.\nl.3 \\foo\n         \n\n"


@pytest.mark.parametrize(
    ("printed", "src_dir", "expected"),
    [
        ("./sections/a.tex", "/data/projects/p", "sections/a.tex"),
        ("././main.tex", "/data/projects/p", "main.tex"),
        ("/data/projects/p/sections/a.tex", "/data/projects/p", "sections/a.tex"),
        ("/data/projects/other/a.tex", "/data/projects/p", None),
        ("/usr/share/texlive/texmf-dist/tex/latex/base/article.cls", "/data/projects/p", None),
        ("C:/Users/Ann/data/projects/p/main.tex", "c:/users/ann/data/projects/p", "main.tex"),
        ("/data/build/p/out/main.aux", "/data/projects/p", None),
        ("./main.aux", "/data/projects/p", None),
        ("sections/a.tex", "/data/projects/p", "sections/a.tex"),
        ("../outside.tex", "/data/projects/p", None),
    ],
)
def test_error_paths_map_to_project_paths(printed: str, src_dir: str, expected: str | None) -> None:
    [found] = parse_log(_UNDEFINED.format(path=printed), src_dir=Path(src_dir))
    assert found.kind == "undefined_control_sequence"
    assert (found.file, found.line) == (expected, 3 if expected else None)


@pytest.mark.parametrize(
    ("opened", "expected"),
    [
        ("(./sections/a.tex", "sections/a.tex"),
        ('("./my file.tex"', "my file.tex"),  # MiKTeX quotes names with spaces
        ("(./sections/my file.tex", "sections/my file.tex"),  # TeX Live does not
        ("(sections/a.tex", "sections/a.tex"),  # a bare name, as MiKTeX prints it
        ("(/data/projects/p/sections/a.tex", "sections/a.tex"),
        ("(/usr/share/texlive/texmf-dist/tex/latex/base/size10.clo", None),
    ],
)
def test_warnings_take_their_file_from_the_stack(opened: str, expected: str | None) -> None:
    log = (
        "(./main.tex\n"
        f"{opened}\n"
        "\n"
        "LaTeX Warning: Reference `x' on page 1 undefined on input line 7.\n"
        "\n"
        ")\n"
        "LaTeX Warning: Citation `y' on page 1 undefined on input line 9.\n"
        ")\n"
    )
    reference, citation = parse_log(log, src_dir=Path("/data/projects/p"))
    assert (reference.file, reference.line) == (expected, 7 if expected else None)
    assert (citation.kind, citation.file, citation.line) == ("undefined_citation", "main.tex", 9)


def test_parentheses_that_are_not_files_stay_balanced() -> None:
    log = (
        "(./main.tex (preloaded format=pdflatex) (Font) size <10> (12.3pt)\n"
        "File: size10.clo 2023/05/17 v1.4n Standard LaTeX file (size option)\n"
        "LaTeX Warning: Reference `x' on page 1 undefined on input line 4.\n"
    )
    [found] = parse_log(log, src_dir=Path("/p"))
    assert (found.file, found.line) == ("main.tex", 4)


def test_bang_errors_take_file_from_the_stack_and_line_from_context() -> None:
    log = (
        "(./main.tex (./sections/intro.tex\n"
        "! Undefined control sequence.\n"
        "l.12 Some text \\foo\n"
        "                    (unbalanced\n"
        "The control sequence at the end of the top line\n"
        "\n"
        ")\n"
        "LaTeX Warning: Reference `x' on page 1 undefined on input line 20.\n"
    )
    error, warning = parse_log(log, src_dir=Path("/p"))
    assert (error.kind, error.file, error.line) == (
        "undefined_control_sequence",
        "sections/intro.tex",
        12,
    )
    assert error.message == "Undefined control sequence.\nl.12 Some text \\foo"
    assert (warning.file, warning.line) == ("main.tex", 20)


def test_package_error_continuation_lines_are_joined() -> None:
    log = (
        "(./main.tex\n"
        "./main.tex:2: Package babel Error: Unknown option `foo'. Either you misspelled it\n"
        "(babel)                or the language definition file foo.ldf was not found.\n"
        "\n"
        "l.2 \\begin\n"
        "           {document}\n"
    )
    [found] = parse_log(log, src_dir=Path("/p"))
    assert found.kind == "package_error"
    assert "or the language definition file foo.ldf was not found." in found.message


# -- dedupe and cap ------------------------------------------------------------


def test_duplicates_are_dropped() -> None:
    warning = "LaTeX Warning: Reference `x' on page 1 undefined on input line 4.\n\n"
    found = parse_log("(./main.tex\n" + warning * 300, src_dir=Path("/p"))
    assert len(found) == 1


def test_cap_keeps_errors_first_and_log_order() -> None:
    boxes = "".join(
        f"Overfull \\hbox (1.0pt too wide) in paragraph at lines {n}--{n}\n []\n\n"
        for n in range(1, 301)
    )
    error = "./main.tex:400: Undefined control sequence.\nl.400 \\foo\n      \n\n"
    found = parse_log("(./main.tex\n" + boxes + error, src_dir=Path("/p"))
    assert len(found) == MAX_DIAGNOSTICS
    assert found[-1].kind == "undefined_control_sequence"
    assert [d.line for d in found[:-1]] == list(range(1, MAX_DIAGNOSTICS))


def test_combine_dedupes_across_parsers() -> None:
    one = Diagnostic("main.tex", 3, "error", "tex_error", "Missing $ inserted.")
    two = Diagnostic(None, None, "warning", "bibtex_warning", "empty year in x")
    assert combine_diagnostics([one, two], [two, one]) == [one, two]


def test_line_is_dropped_when_the_file_is_not_a_project_file() -> None:
    log = "(/usr/share/texlive/texmf-dist/tex/latex/foo/foo.sty\n"
    log += "LaTeX Warning: Something on input line 33.\n"
    [found] = parse_log(log, src_dir=Path("/p"))
    assert (found.file, found.line) == (None, None)


def test_diagnostic_to_dict() -> None:
    found = Diagnostic("main.tex", 3, "error", "tex_error", "Missing $ inserted.")
    assert found.to_dict() == {
        "file": "main.tex",
        "line": 3,
        "severity": "error",
        "kind": "tex_error",
        "message": "Missing $ inserted.",
    }
