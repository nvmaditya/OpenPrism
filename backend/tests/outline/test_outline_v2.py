from __future__ import annotations

from collections.abc import Mapping

from fastapi.testclient import TestClient

from prismtex.outline import LabelInfo, Outline, build_outline
from prismtex.workspace import ProjectStore

FILES = {
    "main.tex": "\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}\n"
    "\\section{Intro}\\label{sec:intro}\nText.\n\\input{sections/method}\n"
    "\\section{End}\nBye.\n\\end{document}\n",
    "sections/method.tex": "\\section{Method}\nWe do X.\n\\subsection{Setup}\nY.\n",
    "orphan.tex": "\\section{Unused}\n",
}


def outline_of(files: Mapping[str, str], root_file: str = "main.tex") -> Outline:
    return build_outline(files.__getitem__, files, root_file)


def spans(outline: Outline) -> list[tuple[str, int, int]]:
    return [(h.title, h.line, h.end_line) for h in outline.headings]


def test_outline_heading_spans_follow_the_input_graph_from_the_root_file() -> None:
    o = build_outline(FILES.__getitem__, FILES, "main.tex")
    assert [(h.path, h.line, h.end_line, h.level, h.title) for h in o.headings] == [
        ("main.tex", 4, 5, 1, "Intro"),
        ("sections/method.tex", 1, 4, 1, "Method"),
        ("sections/method.tex", 3, 4, 2, "Setup"),
        ("main.tex", 7, 8, 1, "End"),
    ]
    assert o.headings[0].label == "sec:intro"
    assert o.inputs == {"main.tex": ("sections/method.tex",)}
    assert o.unincluded == ("orphan.tex",)
    assert o.order("sections/method.tex", 2) < o.order("main.tex", 7)
    assert o.packages == frozenset({"amsmath"}) and o.document_class == "article"


def test_comments_and_verbatim_do_not_produce_headings() -> None:
    main = (
        "\\begin{document}\n"
        "% \\section{Commented}\n"
        "50\\% done \\section{Real}\n"
        "a line break \\\\% \\section{After a line break}\n"
        "\\begin{verbatim}\n\\section{Verbatim}\n\\end{verbatim}\n"
        "\\begin{lstlisting}[language=TeX]\n\\section{Listing}\n\\end{lstlisting}\n"
        "\\begin{minted}{latex}\n\\section{Minted}\n\\end{minted}\n"
        "Inline \\verb|\\section{Verb}| and \\verb%\\section{Pct}% \\subsection{Kept}\n"
        "\\end{document}\n"
    )
    o = outline_of({"main.tex": main})
    assert [(h.title, h.line) for h in o.headings] == [("Real", 3), ("Kept", 14)]
    assert [(e.name, e.start, e.end) for e in o.environments] == [
        ("document", 1, 15),
        ("verbatim", 5, 7),
        ("lstlisting", 8, 10),
        ("minted", 11, 13),
    ]


def test_starred_and_short_title_headings() -> None:
    main = (
        "\\begin{document}\n"
        "\\section*{Skills}\n"
        "\\section[S]{Long title}\n"
        "\\subsection*[x]{Starred short}\n"
        "\\part{P}\n\\chapter{C}\n\\paragraph{Para}\n\\subparagraph{Sub}\n"
        "\\section{About \\emph{this}\\label{sec:about}}\n"
        "\\end{document}\n"
    )
    o = outline_of({"main.tex": main})
    assert [(h.title, h.level) for h in o.headings] == [
        ("Skills", 1),
        ("Long title", 1),
        ("Starred short", 2),
        ("P", -1),
        ("C", 0),
        ("Para", 4),
        ("Sub", 5),
        ("About \\emph{this}", 1),
    ]
    assert o.headings[-1].label == "sec:about"
    assert [h.index for h in o.headings] == list(range(1, 9))


def test_labels_count_references_and_bib_keys_and_cites_are_collected() -> None:
    files = {
        "main.tex": (
            "\\documentclass{article}\n"
            "\\usepackage{natbib, hyperref}\n"
            "\\begin{document}\n"
            "\\section{Intro}\n"
            "\n"
            "\\label{sec:intro}\n"
            "See \\ref{eq:one} and \\eqref{eq:one}; \\cref{sec:intro} again.\n"
            "\\begin{equation}\\label{eq:one} x = 1 \\end{equation}\n"
            "As \\cite{knuth}, \\citep[see][p.~2]{lamport, knuth} and \\citet*{gold}.\n"
            "% \\cite{commented} \\ref{eq:one}\n"
            "\\bibliographystyle{plain}\n"
            "\\bibliography{refs,extra/more}\n"
            "\\end{document}\n"
        ),
        "refs.bib": (
            '@article{knuth,\n  title = {T}\n}\n@string{foo = "bar"}\n'
            "@Book{lamport ,\n}\n@comment{ignored,}\n"
        ),
        "extra/more.bib": "@misc{gold, note = {x}}\n",
        "unused.bib": "@misc{unused, note = {x}}\n",
    }
    o = outline_of(files)
    assert o.headings[0].label == "sec:intro"  # on the next non-blank line
    assert list(o.labels.items()) == [
        ("sec:intro", LabelInfo("sec:intro", "main.tex", 6, refs=1)),
        ("eq:one", LabelInfo("eq:one", "main.tex", 8, refs=2)),
    ]
    assert o.bib_files == ("refs.bib", "extra/more.bib")
    assert o.bib_keys == frozenset({"knuth", "lamport", "gold"})
    assert o.cited == frozenset({"knuth", "lamport", "gold"})
    assert o.packages == frozenset({"natbib", "hyperref"})


def test_environment_spans_nest_and_paragraph_at_splits_on_blank_lines() -> None:
    main = (
        "\\begin{document}\n"  # 1
        "First paragraph,\n"  # 2
        "still the first.\n"  # 3
        "\n"  # 4
        "Second paragraph.\n"  # 5
        "\\begin{figure}\n"  # 6
        "  \\begin{center}\n"  # 7
        "    \\includegraphics{x}\n"  # 8
        "  \\end{center}\n"  # 9
        "  \\caption{A $\\begin{matrix}1\\end{matrix}$ caption}\n"  # 10
        "\\end{figure}\n"  # 11
        "Third, after the figure.\n"  # 12
        "\\end{document}\n"  # 13
    )
    o = outline_of({"main.tex": main})
    assert [(e.name, e.start, e.end) for e in o.environments] == [
        ("document", 1, 13),
        ("figure", 6, 11),
        ("center", 7, 9),
        ("matrix", 10, 10),
    ]
    figure, center = o.environments[1], o.environments[2]
    assert figure.start < center.start <= center.end < figure.end
    assert o.paragraph_at("main.tex", 2) == (2, 3) == o.paragraph_at("main.tex", 3)
    assert o.paragraph_at("main.tex", 5) == (5, 5)
    assert o.paragraph_at("main.tex", 8) == (8, 8)
    assert o.paragraph_at("main.tex", 10) == (10, 10)  # an environment closed on its line
    assert o.paragraph_at("main.tex", 12) == (12, 12)
    for boundary in (1, 4, 6, 9, 11, 13):
        assert o.paragraph_at("main.tex", boundary) is None
    assert o.paragraph_at("main.tex", 99) is None
    assert o.paragraph_at("other.tex", 1) is None


def test_find_heading_matches_label_index_and_title_similarity() -> None:
    o = outline_of(FILES)
    assert [h.title for h in o.find_heading("sec:intro")] == ["Intro"]
    assert [h.title for h in o.find_heading("2")] == ["Method"]
    assert [h.title for h in o.find_heading("methods")] == ["Method"]
    assert [h.title for h in o.find_heading("  SETUP ")] == ["Setup"]
    assert o.find_heading("Conclusion") == () and o.find_heading("9") == ()
    assert o.find_heading("") == ()

    files = {
        "main.tex": "\\begin{document}\n\\section{Result}\n\\section{Results}\n"
        "\\section{\\emph{Related} Work}\n\\end{document}\n"
    }
    o = outline_of(files)
    assert [h.title for h in o.find_heading("results")] == ["Results", "Result"]
    assert [h.title for h in o.find_heading("related work")] == ["\\emph{Related} Work"]


def test_last_section_ends_before_end_document_and_bibliography() -> None:
    main = (
        "\\documentclass{article}\n"  # 1
        "\\begin{document}\n"  # 2
        "\\section{Body}\n"  # 3
        "Text.\n"  # 4
        "\n"  # 5
        "\\section{Last}\n"  # 6
        "Final words.\n"  # 7
        "\n"  # 8
        "\n"  # 9
        "\\bibliographystyle{plain}\n"  # 10
        "\\bibliography{refs}\n"  # 11
        "\\end{document}\n"  # 12
    )
    assert spans(outline_of({"main.tex": main})) == [("Body", 3, 4), ("Last", 6, 7)]

    biblatex = "\\begin{document}\n\\section{Only}\nA.\n\\printbibliography\n\\end{document}\n"
    assert spans(outline_of({"main.tex": biblatex})) == [("Only", 2, 3)]

    plain = "\\begin{document}\n\\section{Only}\nA.\n\n\\end{document}\n"
    assert spans(outline_of({"main.tex": plain})) == [("Only", 2, 3)]

    # A section file has no \end{document}: its last heading runs to its last non-blank line.
    files = {
        "main.tex": "\\begin{document}\n\\input{s}\n\\end{document}\n",
        "s.tex": "\\section{S}\nA.\n\n",
    }
    assert spans(outline_of(files)) == [("S", 1, 2)]


def test_input_cycle_and_missing_file_do_not_hang() -> None:
    files = {
        "main.tex": "\\begin{document}\n\\input{a}\n\\input{missing}\n\\input{binary}\n"
        "\\include{b}\n\\input{a}\n\\input{../outside}\n\\end{document}\n",
        "a.tex": "\\section{A}\n\\input{b}\n",
        "b.tex": "\\section{B}\n\\input{main}\n\\input{a.tex}\nB text.\n",
        "binary.tex": "",
    }

    def read(path: str) -> str:
        if path == "binary.tex":
            raise ValueError("not UTF-8")
        return files[path]

    o = build_outline(read, files, "main.tex")
    assert [(h.path, h.title, h.line, h.end_line) for h in o.headings] == [
        ("a.tex", "A", 1, 1),  # \input{b} on line 2 brings in a heading of the same level
        ("b.tex", "B", 1, 4),
    ]
    assert o.inputs == {
        "main.tex": ("a.tex", "binary.tex", "b.tex"),
        "a.tex": ("b.tex",),
        "b.tex": ("main.tex", "a.tex"),
    }
    assert o.unincluded == ()


def test_outline_route_still_returns_headings_with_lines(
    client: TestClient, store: ProjectStore
) -> None:
    pid = client.post("/api/projects", json={"kind": "paper"}).json()["id"]
    response = client.get(f"/api/projects/{pid}/outline")
    assert response.status_code == 200
    items = response.json()["outline"]
    assert items and all(set(item) == {"title", "line", "level", "macro", "path"} for item in items)
    assert ("sections/introduction.tex", "Introduction", 1, 1, "section") in [
        (i["path"], i["title"], i["line"], i["level"], i["macro"]) for i in items
    ]

    # The route and the Outline agree on every heading of the template.
    paths = [entry.path for entry in store.list_files(pid)]
    o = build_outline(lambda p: store.read(pid, p).text, paths, "main.tex")
    assert sorted((i["path"], i["line"], i["title"]) for i in items) == sorted(
        (h.path, h.line, h.title) for h in o.headings
    )


def test_paper_template_outline_reads_the_document_in_order(store: ProjectStore) -> None:
    pid = store.create(kind="paper").id
    paths = [entry.path for entry in store.list_files(pid)]
    o = build_outline(lambda p: store.read(pid, p).text, paths, "main.tex")
    assert [(h.title, h.label) for h in o.headings] == [
        ("Introduction", "sec:introduction"),
        ("Method", "sec:method"),
        ("Results", "sec:results"),
        ("Conclusion", "sec:conclusion"),
    ]
    assert o.bib_files == ("references.bib",)
    assert o.cited and o.cited <= o.bib_keys
    assert o.labels["eq:mean"].refs == 2
    assert o.unincluded == () and o.document_class == "article"


def test_section_at_follows_input_files_and_order_places_them_after_the_input_line() -> None:
    files = {**FILES, "main.tex": FILES["main.tex"].replace("Text.\n", "Text.\n\\input{fig}\n")}
    files["fig.tex"] = "\\begin{figure}\nX\n\\end{figure}\n"
    o = outline_of(files)
    assert [h.title for h in o.headings] == ["Intro", "Method", "Setup", "End"]
    assert o.headings[0].end_line == 6  # the \input{fig} line belongs to Intro

    def title_at(path: str, line: int) -> str | None:
        heading = o.section_at(path, line)
        return None if heading is None else heading.title

    assert title_at("main.tex", 2) is None  # the preamble
    assert title_at("main.tex", 5) == "Intro"
    assert title_at("fig.tex", 2) == "Intro"  # a file with no heading, included inside Intro
    assert title_at("sections/method.tex", 2) == "Method"
    assert title_at("sections/method.tex", 4) == "Setup"
    assert title_at("main.tex", 9) == "End"
    assert title_at("orphan.tex", 1) is None

    assert o.order("main.tex", 6) < o.order("fig.tex", 1) < o.order("sections/method.tex", 1)
    assert o.order("sections/method.tex", 4) < o.order("main.tex", 8)
    assert o.order("orphan.tex", 1) > o.order("main.tex", 10)
    assert o.order("main.tex", 99) == o.order("main.tex", 10)


def test_headings_in_the_preamble_and_back_matter_boundaries() -> None:
    main = (
        "\\documentclass{book}\n"  # 1
        "\\newcommand{\\mysec}[1]{\\section{#1}}\n"  # 2
        "\\titleformat{\\section}{\\bfseries}{}{0pt}{}\n"  # 3
        "\\begin{document}\n"  # 4
        "\\chapter{Main}\n"  # 5
        "Body.\n"  # 6
        "\\appendix\n"  # 7
        "\\chapter{Proofs}\n"  # 8
        "P.\n"  # 9
        "\\begin{thebibliography}{9}\n"  # 10
        "\\bibitem{k} K.\n"  # 11
        "\\end{thebibliography}\n"  # 12
        "\\end{document}\n"  # 13
    )
    assert spans(outline_of({"main.tex": main})) == [("Main", 5, 6), ("Proofs", 8, 9)]

    # An \input that brings in back matter ends the section before it, too.
    files = {
        "main.tex": "\\begin{document}\n\\section{Last}\nA.\n\\input{back}\n\\end{document}\n",
        "back.tex": "\\bibliography{refs}\n",
    }
    assert spans(outline_of(files)) == [("Last", 2, 3)]


def test_digest_covers_the_files_read_and_the_file_list() -> None:
    base = outline_of(FILES).digest
    assert len(base) == 64 and outline_of(dict(FILES)).digest == base
    changed = {**FILES, "sections/method.tex": FILES["sections/method.tex"] + "More.\n"}
    assert outline_of(changed).digest != base
    assert outline_of({**FILES, "new.tex": ""}).digest != base
    assert outline_of({**FILES, "orphan.tex": "edited, never read"}).digest == base


def test_missing_root_file_gives_an_empty_outline() -> None:
    o = outline_of({"other.tex": "\\section{X}\n"}, "main.tex")
    assert o.headings == () and o.inputs == {} and o.unincluded == ("other.tex",)
    assert o.section_at("other.tex", 1) is None and o.paragraph_at("other.tex", 1) is None
