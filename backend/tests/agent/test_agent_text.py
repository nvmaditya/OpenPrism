from __future__ import annotations

import hashlib

import pytest

from prismtex.agent.text import (
    Ambiguous,
    Applied,
    Closest,
    EscapeDamage,
    NotFound,
    OutOfRange,
    closest_match,
    control_char,
    escape_damage,
    insert_after_line,
    known_commands,
    render_view,
    replace_lines,
    str_replace,
    view_id,
)

TEXT = (
    "".join(f"line {i}\n" for i in range(1, 41))
    + "We train the model for 10 epochs with a learning rate of\n"
    + "$10^{-3}$ and a batch size of 64.\n\\label{sec:training}\n"
)


def test_closest_match_quotes_numbered_raw_lines_never_repr() -> None:
    old = (
        "We train the model for 12 epochs with a learning rate of\n"
        "$10^{-3}$ and a batch size of 64.\n\\label{sec:training}"
    )
    result = str_replace(TEXT, old, "x")
    assert isinstance(result, NotFound)
    c = result.closest
    assert c is not None
    assert (c.start, c.end) == (41, 43) and c.similarity >= 0.8
    assert c.numbered == (
        "  41| We train the model for 10 epochs with a learning rate of\n"
        "  42| $10^{-3}$ and a batch size of 64.\n"
        "  43| \\label{sec:training}"
    )
    assert "\\\\" not in c.numbered and "\\n" not in c.numbered and "'" not in c.numbered


def test_ladder_exact_then_trailing_whitespace_then_collapsed_and_names_the_rung() -> None:
    exact = str_replace("a\n  foo bar\nb\n", "foo bar", "baz")
    assert exact == Applied(text="a\n  baz\nb\n", start_line=2, end_line=2, note=None)

    # Both tolerant rungs match here; the trailing-whitespace rung is tried first.
    trailing = str_replace("a\nfoo bar  \nb\n", "foo bar\nb", "X\nY")
    assert trailing == Applied(
        text="a\nX\nY\n",
        start_line=2,
        end_line=3,
        note="matched ignoring trailing whitespace at lines 2-3",
    )

    text = "\\begin{itemize}\n\t\\item  one\n\t\\item two\n\\end{itemize}\n"
    collapsed = str_replace(text, "    \\item one\n    \\item two", "\\item uno\n\t\\item dos")
    assert collapsed == Applied(
        text="\\begin{itemize}\n\t\\item uno\n\t\\item dos\n\\end{itemize}\n",
        start_line=2,
        end_line=3,
        note="matched ignoring indentation at lines 2-3",
    )

    single = str_replace("x\n  y  z\n", "y z", "w")
    assert single == Applied(
        text="x\n  w\n", start_line=2, end_line=2, note="matched ignoring indentation at line 2"
    )


def test_tolerant_match_keeps_the_files_first_line_indentation() -> None:
    text = "\\begin{proof}\n\t\tWe show  it.\n\t\tDone.\n\\end{proof}\n"
    result = str_replace(text, "We show it.\nDone.", "  We prove it.\n  Done.")
    assert isinstance(result, Applied)
    # Only the first line is re-indented; the rest is the model's text as given.
    assert result.text == "\\begin{proof}\n\t\tWe prove it.\n  Done.\n\\end{proof}\n"
    assert result.note == "matched ignoring indentation at lines 2-3"


def test_ambiguous_match_lists_every_line_and_changes_nothing() -> None:
    lines = [f"filler {i}" for i in range(1, 81)]
    for n in (12, 40, 77):
        lines[n - 1] = "as shown by \\cite{smith}."
    text = "\n".join(lines) + "\n"
    assert str_replace(text, "\\cite{smith}", "\\cite{jones}") == Ambiguous(lines=(12, 40, 77))
    # Two matches on a tolerant rung are ambiguous too.
    assert str_replace("  a\nb\n\ta\n", "a  ", "c") == Ambiguous(lines=(1, 3))


KNOWN = known_commands([])


@pytest.mark.parametrize(
    ("value", "damage"),
    [
        # Each value is what a JSON decoder made of single-escaped LaTeX.
        ("Hello \textbf{world}", EscapeDamage(1, "tab_letters", "TAB+'extbf'", "\\textbf")),
        ("$\x08eta$", EscapeDamage(1, "control_char", "BS+'eta'", "\\beta")),
        ("$\x0crac{1}{2}$", EscapeDamage(1, "control_char", "FF+'rac'", "\\frac")),
        ("see \ref{fig:a}", EscapeDamage(1, "control_char", "CR+'ef'", "\\ref")),
        ("a\nb\nc\newline d", EscapeDamage(3, "lf_letters", "LF+'ewline'", "\\newline")),
        ("$\nu$", EscapeDamage(1, "lf_letters", "LF+'u'", "\\nu")),
        ("first\\n second", EscapeDamage(1, "literal_backslash_n", "\\n", None)),
        ("a\r\nb", EscapeDamage(1, "control_char", "CR", None)),
        ("\the cat", None),
        ("see\ne.g. this", None),
        ("a \\newline b", None),
        ("x \\\\n y", None),
    ],
)
def test_escape_damage_table(value: str, damage: EscapeDamage | None) -> None:
    assert escape_damage(value, KNOWN) == damage


def test_known_commands_include_project_definitions() -> None:
    value = "\tsum_{i} x_i"
    assert escape_damage(value, known_commands([])) is None
    known = known_commands(["\\newcommand{\\tsum}{\\sum_t}\n"])
    assert escape_damage(value, known) == EscapeDamage(1, "tab_letters", "TAB+'sum'", "\\tsum")

    defined = known_commands(
        [
            "\\renewcommand*{\\vect}[1]{\\mathbf{#1}}\n\\def\\eps{\\varepsilon}\n",
            "\\DeclareMathOperator*{\\argmax}{arg\\,max}\n\\newcommand\\bigO{O}\n",
            "% \\newcommand{\\unused}{x}\n",
        ]
    )
    assert {"vect", "eps", "argmax", "bigO", "textbf"} <= defined
    assert "unused" not in defined


def test_view_id_changes_after_an_edit_and_view_caps_at_250_lines() -> None:
    text = "".join(f"line {i}\n" for i in range(1, 301))
    vid = view_id(text)
    assert vid == hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]

    view = render_view("main.tex", text)
    assert (view.path, view.start, view.end, view.total, view.view_id) == (
        "main.tex",
        1,
        250,
        300,
        vid,
    )
    body = view.body.split("\n")
    assert body[0] == f"main.tex · lines 1-250 of 300 · view {vid}"
    assert len(body) == 251
    assert body[1] == "    1| line 1" and body[-1] == "  250| line 250"

    assert render_view("main.tex", text, 8, 10).body.split("\n")[1] == "   8| line 8"
    tail = render_view("main.tex", text, 295, 400)
    assert (tail.start, tail.end) == (295, 300)
    capped = render_view("main.tex", text, 10, 600)
    assert (capped.start, capped.end) == (10, 259)

    edited = replace_lines(text, 5, 5, "line five").text
    assert view_id(edited) != vid
    assert render_view("main.tex", edited).view_id == view_id(edited)


def test_control_char_returns_the_offset_of_the_first_c0_other_than_tab_and_lf() -> None:
    assert control_char("a\tb\nc") is None
    assert control_char("ab\x08c\x01") == 2
    assert control_char("x\r\n") == 1
    assert control_char("\x00") == 0
    assert control_char("end\x1f") == 3
    assert control_char("del\x7f and nbsp\xa0") is None


def test_insert_after_line_zero_is_the_top_and_out_of_range_is_reported() -> None:
    text = "a\nb\n"
    assert insert_after_line(text, 0, "x") == Applied(text="x\na\nb\n", start_line=1, end_line=1)
    assert insert_after_line(text, 2, "x\ny\n") == Applied(
        text="a\nb\nx\ny\n", start_line=3, end_line=4
    )
    assert insert_after_line("a", 1, "b") == Applied(text="a\nb", start_line=2, end_line=2)
    assert insert_after_line(text, 3, "x") == OutOfRange(total=2)
    assert insert_after_line(text, -1, "x") == OutOfRange(total=2)


def test_replace_lines_replaces_deletes_and_inserts_whole_lines() -> None:
    text = "a\nb\nc\n"
    assert replace_lines(text, 2, 2, "B1\nB2") == Applied(
        text="a\nB1\nB2\nc\n", start_line=2, end_line=3
    )
    assert replace_lines(text, 2, 3, "") == Applied(text="a\n", start_line=2, end_line=1)
    assert replace_lines(text, 4, 3, "d\n") == Applied(
        text="a\nb\nc\nd\n", start_line=4, end_line=4
    )
    assert replace_lines("a\nb", 2, 2, "c\n").text == "a\nc"
    # An empty last line keeps its line break even in a text that had none.
    assert replace_lines("a\nb", 2, 2, "\n").text == "a\n\n"
    with pytest.raises(ValueError):
        replace_lines(text, 3, 4, "x")


def test_closest_match_prefers_the_first_window_on_a_tie() -> None:
    c = closest_match("alpha\nbeta\nalpha\nbeta\n", "alpha\nbetx")
    assert (c.start, c.end) == (1, 2)
    assert closest_match("", "x") == Closest(start=1, end=0, similarity=0.0, numbered="")
    assert str_replace("", "x", "y") == NotFound(closest=None)
    assert str_replace("a\n", "", "y") == NotFound(closest=None)
