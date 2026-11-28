"""The log parser's final Diagnostic kind set (agent design §7.3).

miktex-kinds.log.txt is the log MiKTeX 25.12 (pdfTeX 1.40.28) wrote on
2026-11-28 for the document below, compiled with the hardened command; the
MiKTeX installation and build-folder paths were shortened. texlive-kinds
.log.txt is the same document in TeX Live 2023's wording, assembled from the
TeX Live 2023 logs of the other fixtures (no TeX Live was at hand), with the
build folder rewritten to /data/build/texlive-kinds/out.

    1 \\documentclass{article}
    2 \\begin{document}
    3 See \\cite{nokey} and \\ref{sec:none}.
    4 Here is \\foo{} an undefined macro.
    5 \\begin{nosuchenv}
    6 Inside.
    7 \\end{nosuchenv}
    8 \\input{nothere}
    9 \\end{document}
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prismtex.compile import parse_log

FIXTURES = Path(__file__).parent.parent / "fixtures" / "texlogs"

# The kinds the TeX log parser can emit. The verifier relies on the first
# nine (agent design §7.3); the rest are finer kinds of TeX errors and LaTeX
# warnings that the compile docs list.
FINAL_KINDS = {
    "tex_error",
    "undefined_control_sequence",
    "undefined_environment",
    "fatal",
    "latex_warning",
    "package_warning",
    "undefined_reference",
    "undefined_citation",
    "overfull_box",
    "underfull_box",
    "missing_file",
    "latex_error",
    "package_error",
    "multiply_defined_label",
    "font_warning",
}


@pytest.mark.parametrize("case", ["texlive-kinds", "miktex-kinds"])
def test_log_parser_emits_the_final_kind_set(case: str) -> None:
    text = (FIXTURES / f"{case}.log.txt").read_text(encoding="utf-8")
    found = parse_log(text, src_dir=Path(f"/data/projects/{case}"))
    located = {(d.kind, d.file, d.line) for d in found}
    for expected in [
        ("undefined_citation", "main.tex", 3),
        ("undefined_reference", "main.tex", 3),
        ("undefined_control_sequence", "main.tex", 4),
        ("undefined_environment", "main.tex", 5),
        ("fatal", "main.tex", 8),
    ]:
        assert expected in located, (expected, found)
    messages = {d.kind: d.message for d in found}
    assert messages["undefined_control_sequence"] == "Undefined control sequence \\foo"
    assert messages["undefined_environment"] == "Environment nosuchenv undefined"
    assert {d.kind for d in found} <= FINAL_KINDS


def test_undefined_control_sequence_names_the_command_at_the_end_of_the_top_line() -> None:
    # Inside a macro, the top context line is the expansion, not the l.N line;
    # the undefined command is the one at its end.
    log = (
        "(./main.tex\n"
        "./main.tex:7: Undefined control sequence.\n"
        "\\greet #1->Hello \\undefinedname \n"
        "                                {#1}\n"
        "l.7 \\greet{world}\n"
        "                \n"
        "The control sequence at the end of the top line\n"
        "\n"
    )
    [found] = parse_log(log, src_dir=Path("/p"))
    assert (found.kind, found.file, found.line) == ("undefined_control_sequence", "main.tex", 7)
    assert found.message == "Undefined control sequence \\undefinedname"


@pytest.mark.parametrize(
    ("top_line", "command"),
    [
        ("l.3 \\section{\\foo", "\\foo"),
        ("l.3 \\__prismtex_missing:n", "\\__prismtex_missing:n"),
        ("<recently read> \\foo ", "\\foo"),
    ],
)
def test_undefined_command_is_taken_from_the_top_line(top_line: str, command: str) -> None:
    log = f"(./main.tex\n./main.tex:3: Undefined control sequence.\n{top_line}\n   x\n\n"
    [found] = parse_log(log, src_dir=Path("/p"))
    assert found.message == f"Undefined control sequence {command}"


def test_undefined_control_sequence_without_a_context_line_keeps_the_plain_message() -> None:
    [found] = parse_log("(./main.tex\n! Undefined control sequence.\n", src_dir=Path("/p"))
    assert (found.kind, found.message) == (
        "undefined_control_sequence",
        "Undefined control sequence.",
    )


def test_undefined_environment_from_a_bang_error_takes_its_line_from_the_context() -> None:
    log = (
        "(./main.tex\n"
        "! LaTeX Error: Environment tabularx undefined.\n"
        "\n"
        "See the LaTeX manual or LaTeX Companion for explanation.\n"
        "Type  H <return>  for immediate help.\n"
        " ...                                              \n"
        "                                                  \n"
        "l.12 \\begin{tabularx}\n"
        "                     {\\linewidth}{X}\n"
        "\n"
    )
    [found] = parse_log(log, src_dir=Path("/p"))
    assert (found.kind, found.file, found.line, found.message) == (
        "undefined_environment",
        "main.tex",
        12,
        "Environment tabularx undefined",
    )


def test_overfull_and_underfull_boxes_are_info_with_their_own_kinds() -> None:
    log = (
        "(./main.tex\n"
        "Overfull \\hbox (12.5pt too wide) in paragraph at lines 30--32\n"
        "[]\\OT1/cmr/m/n/10 Word\n"
        "\n"
        "Underfull \\vbox (badness 10000) has occurred while \\output is active []\n"
        "\n"
        "Underfull \\hbox (badness 10000) detected at line 40\n"
        "\n"
    )
    found = parse_log(log, src_dir=Path("/p"))
    assert [(d.severity, d.kind, d.line) for d in found] == [
        ("info", "overfull_box", 30),
        ("info", "underfull_box", None),
        ("info", "underfull_box", 40),
    ]
