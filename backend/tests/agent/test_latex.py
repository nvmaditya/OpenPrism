"""Host LaTeX operations: packages, placement, bibliography, fragments (agent design §4.4)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

import pytest

from prismtex.agent.latex import (
    BibError,
    BibResult,
    Edit,
    add_bib_entry,
    apply_edits,
    bib_keys,
    cite_keys,
    ensure_package,
    environments_balanced,
    insert_after_anchor,
    insertion_point,
    packages_needed,
    split_usepackages,
    strip_fragment_wrappers,
    wire_bibliography,
)
from prismtex.outline import Heading, Outline, build_outline

ROOT = (
    "\\documentclass{article}\n\\usepackage{amsmath}\n\\usepackage[utf8]{inputenc}\n"
    "\\begin{document}\nHi\n\\end{document}\n"
)

ENTRY = "@article{smith2020,\n  title = {Deep Nets},\n  author = {Smith, J.},\n  year = 2020,\n}"
ENTRY_LINES = (
    "@article{smith2020,",
    "  title = {Deep Nets},",
    "  author = {Smith, J.},",
    "  year = 2020,",
    "}",
)

PAPER = {
    "main.tex": (
        "\\documentclass{article}\n"  # 1
        "\\begin{document}\n"  # 2
        "\\section{Intro}\n"  # 3
        "Text.\n"  # 4
        "\\subsection{Detail}\n"  # 5
        "More.\n"  # 6
        "\n"  # 7
        "\\section{Method}\\label{sec:method}\n"  # 8
        "\\input{sections/method}\n"  # 9
        "\\input{sections/results}\n"  # 10
        "\\section{Conclusion}\n"  # 11
        "Done.\n"  # 12
        "\n"  # 13
        "\\bibliographystyle{plain}\n"  # 14
        "\\bibliography{refs}\n"  # 15
        "\\end{document}\n"  # 16
    ),
    "sections/method.tex": "We do X.\n\\subsection{Setup}\n\\label{sec:setup}\nY.\n",
    "sections/results.tex": "\\section{Results}\n\n\\label{sec:results}\nR.\n",
}


def outline_of(project: Mapping[str, str], root_file: str = "main.tex") -> Outline:
    return build_outline(project.__getitem__, project, root_file)


def heading(outline: Outline, title: str) -> Heading:
    return next(h for h in outline.headings if h.title == title)


def wire(project: Mapping[str, str], bib_path: str = "refs.bib") -> tuple[Edit, ...]:
    return wire_bibliography(
        project["main.tex"], outline_of(project), bib_path, root_file="main.tex"
    )


def test_ensure_package_is_idempotent_and_lands_after_the_last_usepackage() -> None:
    e = ensure_package(ROOT, "booktabs", root_file="main.tex")
    assert e == Edit(
        path="main.tex",
        after_line=3,
        lines=("\\usepackage{booktabs}",),
        note="added \\usepackage{booktabs}",
    )
    assert e is not None
    assert ensure_package(apply_edits(ROOT, [e]), "booktabs", root_file="main.tex") is None
    assert (
        ensure_package(
            "\\documentclass{article}\n\\usepackage{amsmath,booktabs}\n",
            "booktabs",
            root_file="main.tex",
        )
        is None
    )
    assert (
        ensure_package(
            "\\documentclass{article}\n\\RequirePackage{booktabs}\n",
            "booktabs",
            root_file="main.tex",
        )
        is None
    )
    graphicx = ensure_package(
        "\\documentclass{article}\n\\begin{document}\n", "graphicx", root_file="main.tex"
    )
    assert graphicx is not None and graphicx.after_line == 1


def test_ensure_package_with_different_options_changes_nothing_and_notes_it() -> None:
    root = (
        "\\documentclass{article}\n\\usepackage[margin=1in, top=2cm]{geometry}\n"
        "\\begin{document}\n\\end{document}\n"
    )
    kept = ensure_package(root, "geometry", "margin=2cm", root_file="main.tex")
    assert kept == Edit(
        path="main.tex",
        after_line=2,
        lines=(),
        note="kept \\usepackage[margin=1in,top=2cm]{geometry}; requested [margin=2cm]",
    )
    assert kept is not None and apply_edits(root, [kept]) == root
    assert ensure_package(root, "geometry", root_file="main.tex") is None
    assert ensure_package(root, "geometry", " margin = 1in ", root_file="main.tex") is None
    unset = ensure_package(ROOT, "amsmath", "fleqn", root_file="main.tex")
    assert unset is not None and unset.lines == ()
    assert unset.note == "kept \\usepackage{amsmath}; requested [fleqn]"
    assert ensure_package(ROOT, "geometry", "margin=1in", root_file="main.tex") == Edit(
        "main.tex",
        3,
        ("\\usepackage[margin=1in]{geometry}",),
        "added \\usepackage[margin=1in]{geometry}",
    )
    for name, options in (
        ("a,b", ""),
        ("", ""),
        ("x}", ""),
        ("geometry", "a]b"),
        ("geometry", "a%b"),
        ("geometry", "{a"),
        ("geometry", "a\nb"),
    ):
        with pytest.raises(ValueError):
            ensure_package(ROOT, name, options, root_file="main.tex")


def test_ensure_package_lands_after_a_multiline_usepackage_and_never_before_the_class() -> None:
    root = (
        "\\RequirePackage{fix-cm}\n"
        "\\documentclass{article}\n"
        "\\usepackage{amsmath,\n"
        "  amssymb}\n"
        "\\title{T}\n"
        "\\begin{document}\n"
        "\\end{document}\n"
    )
    assert ensure_package(root, "booktabs", root_file="paper.tex") == Edit(
        "paper.tex", 4, ("\\usepackage{booktabs}",), "added \\usepackage{booktabs}"
    )
    assert ensure_package(root, "amssymb", root_file="paper.tex") is None
    assert ensure_package(root, "fix-cm", root_file="paper.tex") is None
    guarded = (
        "\\documentclass{article}\n"
        "\\usepackage{amsmath}\n"
        "\\IfFileExists{foo.sty}{\n"
        "  \\usepackage{foo}\n"
        "}{}\n"
        "\\begin{document}\n"
    )
    inside = ensure_package(guarded, "booktabs", root_file="main.tex")
    assert inside is not None and inside.after_line == 2
    assert ensure_package(guarded, "foo", root_file="main.tex") is None
    no_class = ensure_package("Text\n\\begin{document}\n", "x", root_file="m.tex")
    assert no_class is not None and no_class.after_line == 1
    empty = ensure_package("", "x", root_file="m.tex")
    assert empty is not None and empty.after_line == 0


def test_insertion_point_end_is_before_the_next_same_level_heading_and_never_after_bibliography():
    o = outline_of(PAPER)
    assert insertion_point(o, heading(o, "Intro")) == ("main.tex", 6)
    assert insertion_point(o, heading(o, "Detail"), where="end") == ("main.tex", 6)
    # Method's body runs through sections/method.tex; Results arrives with the next \input.
    assert insertion_point(o, heading(o, "Method")) == ("main.tex", 9)
    assert insertion_point(o, heading(o, "Setup")) == ("sections/method.tex", 4)
    assert insertion_point(o, heading(o, "Results")) == ("sections/results.tex", 4)
    conclusion = insertion_point(o, heading(o, "Conclusion"))
    assert conclusion == ("main.tex", 12)
    text = apply_edits(PAPER["main.tex"], [Edit("main.tex", conclusion[1], ("NEW",), "")])
    assert text.index("Done.\nNEW\n") < text.index("\\bibliographystyle")

    for tail in ("\\printbibliography\n\\end{document}\n", "\n\\end{document}\n"):
        last = {"main.tex": "\\begin{document}\n\\section{Last}\nText.\n" + tail}
        lo = outline_of(last)
        assert insertion_point(lo, heading(lo, "Last")) == ("main.tex", 3)

    where: Any = "middle"
    with pytest.raises(ValueError):
        insertion_point(o, heading(o, "Intro"), where=where)
    with pytest.raises(ValueError):
        insertion_point(o, Heading("main.tex", 3, 6, 1, "Elsewhere", None, 99))


def test_insertion_point_start_is_after_the_heading_and_its_label_and_follows_input_files() -> None:
    o = outline_of(PAPER)
    assert insertion_point(o, heading(o, "Intro"), where="start") == ("main.tex", 3)
    assert insertion_point(o, heading(o, "Method"), where="start") == ("main.tex", 8)
    # Headings inside included files give those files: the label on the next line, or after a
    # blank line, is part of the heading.
    assert insertion_point(o, heading(o, "Setup"), where="start") == ("sections/method.tex", 3)
    assert insertion_point(o, heading(o, "Results"), where="start") == (
        "sections/results.tex",
        3,
    )
    assert insertion_point(o, heading(o, "Conclusion"), where="start") == ("main.tex", 11)

    nested = {
        "main.tex": "\\begin{document}\n\\input{intro}\n\\end{document}\n",
        "intro.tex": "\\section{Intro}\n\\label{sec:intro}\nText.\n",
    }
    no = outline_of(nested)
    assert insertion_point(no, heading(no, "Intro"), where="start") == ("intro.tex", 2)
    assert insertion_point(no, heading(no, "Intro"), where="end") == ("intro.tex", 3)


def test_insert_after_anchor_goes_after_the_labelled_environment_or_a_unique_line() -> None:
    text = (
        "\\section{Results}\\label{sec:results}\n"  # 1
        "\\begin{figure}\n"  # 2
        "\\centering\n"  # 3
        "\\begin{minipage}{0.4\\linewidth}\n"  # 4
        "\\includegraphics{a}\n"  # 5
        "\\caption{A}\\label{fig:a}\n"  # 6
        "\\end{minipage}\n"  # 7
        "\\end{figure}\n"  # 8
        "We see a gain.\n"  # 9
        "We see a gain in accuracy.\n"  # 10
        "\\begin{multicols}{2}\n"  # 11
        "\\subsection{Side}\\label{sec:side}\n"  # 12
        "Side text.\n"  # 13
        "\\end{multicols}\n"  # 14
    )
    assert insert_after_anchor(text, "fig:a") == 8
    assert insert_after_anchor(text, "\\label{fig:a}") == 8
    assert insert_after_anchor(text, "sec:results") == 1
    assert insert_after_anchor(text, "sec:side") == 12  # an environment holding a heading
    assert insert_after_anchor(text, "  We see a   gain. ") == 9
    assert insert_after_anchor(text, "in accuracy") == 10
    assert insert_after_anchor(text, "We see") is None
    assert insert_after_anchor(text, "\\label{missing}") is None
    assert insert_after_anchor(text, "nowhere") is None
    assert insert_after_anchor(text, "") is None
    assert insert_after_anchor(text, "two\nlines") is None
    assert insert_after_anchor("\\label{x}\n\\label{x}\n", "x") is None


def test_add_bib_entry_reuses_identical_rejects_a_conflicting_key_and_creates_refs_bib() -> None:
    project = {
        "main.tex": (
            "\\documentclass{article}\n\\begin{document}\nHi.\n"
            "\\bibliography{library}\n\\end{document}\n"
        ),
        "library.bib": "@book{knuth1984,\n  title = {The TeXbook},\n}\n",
        "refs.bib": "",  # not named by the document: never written
    }
    added = add_bib_entry(project, outline_of(project), ENTRY)
    assert added == BibResult(
        key="smith2020",
        path="library.bib",
        edits=(
            Edit(
                "library.bib",
                3,
                ("", *ENTRY_LINES),
                "added bibliography entry smith2020 to library.bib",
            ),
        ),
        created=False,
        reused=False,
    )
    updated = {**project, "library.bib": apply_edits(project["library.bib"], added.edits)}
    assert bib_keys(updated["library.bib"]) == ("knuth1984", "smith2020")

    same = '@ARTICLE{smith2020, year = {2020}, author = "Smith,  J.", title = {Deep Nets}}'
    assert add_bib_entry(updated, outline_of(updated), same) == BibResult(
        key="smith2020", path="library.bib", edits=(), created=False, reused=True
    )
    with pytest.raises(BibError) as conflict:
        add_bib_entry(updated, outline_of(updated), "@article{Smith2020, title = {Shallow}}")
    assert conflict.value.reason == "duplicate_key"
    assert "smith2020" in conflict.value.message and "library.bib" in conflict.value.message

    bare = {"main.tex": "\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}\n"}
    made = add_bib_entry(bare, outline_of(bare), ENTRY)
    assert made == BibResult(
        key="smith2020",
        path="refs.bib",
        edits=(Edit("refs.bib", 0, ENTRY_LINES, "added bibliography entry smith2020 to refs.bib"),),
        created=True,
        reused=False,
    )
    assert apply_edits("", made.edits) == ENTRY + "\n"

    named = {"main.tex": "\\begin{document}\n\\bibliography{lib/papers}\n\\end{document}\n"}
    missing = add_bib_entry(named, outline_of(named), ENTRY)
    assert (missing.path, missing.created) == ("lib/papers.bib", True)


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "smith2020",
        "@article{smith2020 title = {x}}",
        "@article{smith2020, title = {x}",
        "@article{a, title = {x}} @book{b, title = {y}}",
        "@string{venue = {NeurIPS}}",
        "@comment{a, b}",
        "@article{a, title}",
        "@article{a, title = {x}, title = {y}}",
        '@article{a, title = "x}',
        "@article{a, title = {x} year = 2020}",
        "Here it is: @article{a, title = {x}}",
    ],
)
def test_add_bib_entry_rejects_text_that_is_not_exactly_one_entry(entry: str) -> None:
    with pytest.raises(BibError) as error:
        add_bib_entry({}, outline_of({"main.tex": ""}), entry)
    assert error.value.reason == "parse"
    assert error.value.message


def test_wire_bibliography_plain_vs_biblatex_and_noop_when_present() -> None:
    plain = {
        "main.tex": (
            "\\documentclass{article}\n\\begin{document}\nHi \\cite{smith2020}.\n"
            "% \\bibliography{old}\n\\end{document}\n"
        )
    }
    edits = wire(plain)
    assert edits == (
        Edit(
            "main.tex",
            4,
            ("\\bibliographystyle{plain}", "\\bibliography{refs}"),
            "added \\bibliographystyle{plain} and \\bibliography{refs}",
        ),
    )
    wired = {"main.tex": apply_edits(plain["main.tex"], edits), "refs.bib": ENTRY + "\n"}
    assert wired["main.tex"].endswith(
        "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"
    )
    assert wire(wired) == ()

    styled = {"main.tex": "\\begin{document}\n\\bibliographystyle{abbrv}\n\\end{document}\n"}
    assert wire(styled, "bib/refs.bib") == (
        Edit("main.tex", 2, ("\\bibliography{bib/refs}",), "added \\bibliography{bib/refs}"),
    )

    biblatex = {
        "main.tex": (
            "\\documentclass{article}\n"
            "\\usepackage[style=authoryear]{biblatex}\n"
            "\\usepackage{hyperref}\n"
            "\\begin{document}\n"
            "Hi \\textcite{smith2020}.\n"
            "\\end{document}\n"
        )
    }
    edits = wire(biblatex)
    assert edits == (
        Edit("main.tex", 2, ("\\addbibresource{refs.bib}",), "added \\addbibresource{refs.bib}"),
        Edit("main.tex", 5, ("\\printbibliography",), "added \\printbibliography"),
    )
    done = {"main.tex": apply_edits(biblatex["main.tex"], edits), "refs.bib": ""}
    assert "\\addbibresource{refs.bib}\n\\usepackage{hyperref}" in done["main.tex"]
    assert done["main.tex"].endswith("\\printbibliography\n\\end{document}\n")
    assert wire(done) == ()

    # A bibliography is present: in the Root file, or in a file it includes.
    present = {"main.tex": "\\begin{document}\n\\bibliography{refs}\n\\end{document}\n"}
    assert wire(present) == ()
    included = {
        "main.tex": "\\begin{document}\n\\input{back}\n\\end{document}\n",
        "back.tex": "\\bibliography{refs}\n",
    }
    assert wire(included) == ()
    printed = {
        "main.tex": (
            "\\usepackage{biblatex}\n\\begin{document}\n\\printbibliography\n\\end{document}\n"
        )
    }
    assert wire(printed) == (
        Edit("main.tex", 1, ("\\addbibresource{refs.bib}",), "added \\addbibresource{refs.bib}"),
    )

    for bad in ("refs.txt", "my refs.bib", "refs%.bib"):
        with pytest.raises(ValueError):
            wire(plain, bad)


def test_strip_fragment_wrappers_and_split_usepackages() -> None:
    fragment = (
        "\\documentclass[11pt]{article}\n"
        "\\usepackage{booktabs}\n"
        "\\usepackage[table, dvipsnames]{xcolor} % colours\n"
        "\\begin{document}\n"
        "\\begin{table}\n"
        "\\toprule\n"
        "\\end{table}\n"
        "% \\begin{document} stays: it is a comment\n"
        "\\end{document}\n"
    )
    text, removed = strip_fragment_wrappers(fragment)
    assert removed == ("\\documentclass[11pt]{article}", "\\begin{document}", "\\end{document}")
    assert text == (
        "\\usepackage{booktabs}\n"
        "\\usepackage[table, dvipsnames]{xcolor} % colours\n"
        "\\begin{table}\n"
        "\\toprule\n"
        "\\end{table}\n"
        "% \\begin{document} stays: it is a comment\n"
    )
    body, packages = split_usepackages(text)
    assert packages == (("booktabs", ""), ("xcolor", "table,dvipsnames"))
    assert body == (
        "\\begin{table}\n\\toprule\n\\end{table}\n% \\begin{document} stays: it is a comment\n"
    )
    assert strip_fragment_wrappers(body) == (body, ())
    assert split_usepackages(body) == (body, ())

    assert strip_fragment_wrappers("\\begin{document}Hello\n\\end{document}") == (
        "Hello",
        ("\\begin{document}", "\\end{document}"),
    )
    assert split_usepackages("\\usepackage{amsmath,\n  amssymb}Text\n") == (
        "Text\n",
        (("amsmath", ""), ("amssymb", "")),
    )
    assert split_usepackages("\\RequirePackage[T1]{fontenc}\n") == ("", (("fontenc", "T1"),))


def test_packages_needed_maps_macros_and_environments_and_skips_loaded() -> None:
    table = "\\begin{tabular}{ll}\\toprule a & b \\\\ \\midrule\n\\end{tabular}"
    assert packages_needed(table, frozenset()) == ("booktabs",)
    assert packages_needed(table, frozenset({"booktabs"})) == ()
    assert packages_needed("\\begin{align}x\\end{align}", frozenset({"amsmath"})) == ()
    assert packages_needed("\\begin{align*}x\\end{align*}", frozenset()) == ("amsmath",)
    assert packages_needed("\\SI{3}{m}", frozenset()) == ("siunitx",)
    mixed = (
        "\\url{a} \\href{b}{c} \\includegraphics{f} $\\mathbb{R}$ "
        "\\multirow{2}{*}{x} \\textcolor{red}{y}"
    )
    # \href needs hyperref, which provides \url too.
    assert packages_needed(mixed, frozenset()) == (
        "hyperref",
        "graphicx",
        "amssymb",
        "multirow",
        "xcolor",
    )
    assert packages_needed("\\url{a}", frozenset()) == ("url",)
    assert packages_needed("$\\mathbb{R}$", frozenset({"amsfonts"})) == ()
    envs = (
        "\\begin{tabularx}{\\linewidth}{X}a\\end{tabularx}\n\\begin{tikzpicture}\\end{tikzpicture}"
    )
    assert packages_needed(envs, frozenset()) == ("tabularx", "tikz")
    assert packages_needed("\\begin{figure*}\\end{figure*} \\textbf{x}", frozenset()) == ()


def test_package_table_maps_the_design_pairs_to_their_first_package() -> None:
    raw = json.loads(
        files("prismtex.agent").joinpath("data", "packages.json").read_text(encoding="utf-8")
    )
    pairs = {
        "\\toprule": "booktabs",
        "\\midrule": "booktabs",
        "\\includegraphics": "graphicx",
        "\\url": "url",
        "\\href": "hyperref",
        "\\mathbb": "amssymb",
        "align": "amsmath",
        "\\SI": "siunitx",
        "\\multirow": "multirow",
        "tabularx": "tabularx",
        "\\textcolor": "xcolor",
        "tikzpicture": "tikz",
    }
    for key, package in pairs.items():
        assert raw[key][0] == package, key


def test_scanners_ignore_comments_and_escaped_percent() -> None:
    tex = (
        "50\\% of runs \\cite{a,b} % \\cite{hidden}\n"
        "\\begin{itemize} % \\end{itemize}\n"
        "\\item x\\\\% \\cite{after_break}\n"
        "\\end{itemize}\n"
        "\\verb|\\cite{verb}| \\citep[see][p.~3]{c} \\nocite{*}\n"
        "\\begin{verbatim}\n\\begin{figure}\\cite{v}\n\\end{verbatim}\n"
    )
    assert cite_keys(tex) == ("a", "b", "c")
    assert environments_balanced(tex)
    assert environments_balanced("100\\% \\begin{a}\\end{a}")
    assert not environments_balanced("\\begin{figure}\n% \\end{figure}\n")
    assert not environments_balanced("\\begin{a}\\begin{b}\\end{a}\\end{b}")
    assert not environments_balanced("\\end{a}")

    bib = (
        "% @article{commented, title = {x}}\n"
        "@book{kept, title = {50% off}, url = {http://x.org/a%20b}}\n"
        "@string{venue = {NeurIPS}}\n"
        "@comment{@article{nope, title = {z}}}\n"
        "@misc(paren, note = {n})\n"
        "@article{kept, title = {again}}\n"
    )
    assert bib_keys(bib) == ("kept", "paren")

    root = (
        "\\documentclass{article}\n% \\usepackage{booktabs}\n"
        "\\usepackage{amsmath} % \\usepackage{booktabs}\n\\begin{document}\n\\end{document}\n"
    )
    e = ensure_package(root, "booktabs", root_file="main.tex")
    assert e is not None and (e.after_line, e.lines) == (3, ("\\usepackage{booktabs}",))
    assert packages_needed("% \\toprule\n50\\% \\SI{1}{m}", frozenset()) == ("siunitx",)
    assert strip_fragment_wrappers("% \\documentclass{article}\n") == (
        "% \\documentclass{article}\n",
        (),
    )
    assert insert_after_anchor("% \\label{x}\n\\begin{table}\\label{x}\n\\end{table}\n", "x") == 3


def test_apply_edits_keeps_order_after_one_line_and_rejects_bad_edits() -> None:
    assert (
        apply_edits(
            "a\nb\n",
            [
                Edit("f", 1, ("x",), ""),
                Edit("f", 2, ("end",), ""),
                Edit("f", 1, ("y",), ""),
                Edit("f", 0, ("top",), ""),
            ],
        )
        == "top\na\nx\ny\nb\nend\n"
    )
    assert apply_edits("a", [Edit("f", 1, ("b",), "")]) == "a\nb"
    assert apply_edits("", [Edit("f", 0, ("only",), "")]) == "only\n"
    assert apply_edits("a\n", [Edit("f", 1, (), "a note")]) == "a\n"
    assert apply_edits("a\n", []) == "a\n"
    with pytest.raises(ValueError):
        apply_edits("a\n", [Edit("f", 2, ("x",), "")])
    with pytest.raises(ValueError):
        apply_edits("a\n", [Edit("f", 0, ("x",), ""), Edit("g", 0, ("y",), "")])
    with pytest.raises(ValueError):
        Edit("f", 0, ("a\nb",), "")
    with pytest.raises(ValueError):
        Edit("f", -1, ("a",), "")
