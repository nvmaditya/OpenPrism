"""The Outline of a Project: its structure, derived from the Source files.

`build_outline` follows the `\\input` / `\\include` graph from the Root file and
collects headings with line spans and labels, environment spans, labels with
their reference counts, bibliography files and keys, cited keys, loaded
packages, the document class, and the `.tex` files the Root file never
includes (agent design §2.3). It is pure: files are read only through the
callable it is given, and every line number is 1-based.

`parse_tex_outline` and `outline_project` are the older per-file parser. They
define the response of `GET /api/projects/{id}/outline` and are kept as they
were for that route.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import posixpath
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

from prismtex.errors import AppError
from prismtex.workspace import ProjectStore
from prismtex.workspace.text import strip_comment

__all__ = [
    "EnvSpan",
    "Heading",
    "LabelInfo",
    "Outline",
    "build_outline",
    "outline_project",
    "parse_tex_outline",
]


@dataclass(frozen=True)
class Heading:
    """A sectioning command of the document; `line` to `end_line` is its span in `path`."""

    path: str
    line: int
    end_line: int
    level: int  # \part -1, \chapter 0, \section 1, ... \subparagraph 5
    title: str  # the braced title, without a \label inside it
    label: str | None
    index: int  # 1-based, in document order


@dataclass(frozen=True)
class EnvSpan:
    """A closed `\\begin{name}` ... `\\end{name}` pair; `start` and `end` are its lines."""

    path: str
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class LabelInfo:
    """Where a `\\label` key is defined, and how many reference commands name it."""

    key: str
    path: str
    line: int
    refs: int  # \ref, \eqref, \cref, \Cref, \autoref, \pageref


@dataclass(frozen=True)
class _FileIndex:
    """Line data of one file in the document, for the Outline's position queries."""

    n_lines: int
    # Runs of consecutive lines: (first line, last line, position of the first line).
    segments: tuple[tuple[int, int, int], ...]
    firsts: tuple[int, ...]  # the first line of each segment, for bisect
    entry: int  # position where the file's text begins
    exit: int  # position after the file's text and everything it includes
    breaks: frozenset[int]  # lines that are never inside a paragraph


@dataclass(frozen=True)
class _Index:
    files: Mapping[str, _FileIndex] = field(default_factory=lambda: MappingProxyType({}))
    end: int = 0  # position after the whole document


@dataclass(frozen=True)
class Outline:
    """The structure of a Project's document, read from the Root file down.

    Positions from `order` count lines in document order: the lines of an
    included file sit right after the line that includes it.
    """

    root_file: str
    headings: tuple[Heading, ...]  # document order, following the \input graph
    inputs: Mapping[str, tuple[str, ...]]  # file -> the files its \input / \include lines name
    labels: Mapping[str, LabelInfo]  # in document order; the first definition of a key wins
    environments: tuple[EnvSpan, ...]  # document order of their \begin lines
    bib_files: tuple[str, ...]  # from \bibliography and \addbibresource, whether present or not
    bib_keys: frozenset[str]  # entry keys of the bib_files that exist
    cited: frozenset[str]
    packages: frozenset[str]
    document_class: str | None
    unincluded: tuple[str, ...]  # Source .tex files the Root file never includes, by path
    digest: str  # sha256 over the inputs; the cache key
    _index: _Index = field(default_factory=_Index, repr=False, compare=False)

    def order(self, path: str, line: int) -> int:
        """The position of a line in document order.

        A line past either end of its file is clamped to the file. Every line
        of a file outside the document gets the same position, after the
        document's last line.
        """
        return _position(self._index, path, line)

    def section_at(self, path: str, line: int) -> Heading | None:
        """The innermost heading whose span holds the line, following `\\input` files."""
        file = self._index.files.get(path)
        if file is None or not 1 <= line <= file.n_lines:
            return None
        here = self.order(path, line)
        best: tuple[int, int] | None = None
        found: Heading | None = None
        for heading in self.headings:
            start = self.order(heading.path, heading.line)
            if start <= here <= self._extent_end(heading):
                key = (start, heading.index)
                if best is None or key > best:
                    best, found = key, heading
        return found

    def paragraph_at(self, path: str, line: int) -> tuple[int, int] | None:
        """The first and last line of the paragraph holding the line, in that file.

        Paragraphs are split by blank lines, by `\\begin` and `\\end` lines
        (unless the environment closes on the line it opens), and by heading,
        `\\input` and back-matter lines. Such a line itself gives None.
        """
        file = self._index.files.get(path)
        if file is None or not 1 <= line <= file.n_lines or line in file.breaks:
            return None
        start = line
        while start > 1 and start - 1 not in file.breaks:
            start -= 1
        end = line
        while end < file.n_lines and end + 1 not in file.breaks:
            end += 1
        return start, end

    def find_heading(self, query: str) -> tuple[Heading, ...]:
        """Headings a user's words name.

        Tried in order: an exact `\\label` key; a 1-based Outline index such as
        "3"; titles whose case-insensitive similarity is at least 0.8, best
        first. Empty when nothing matches.
        """
        wanted = " ".join(query.split())
        if not wanted:
            return ()
        by_label = tuple(h for h in self.headings if h.label == wanted)
        if by_label:
            return by_label
        if wanted.isascii() and wanted.isdigit():
            number = int(wanted)
            if 1 <= number <= len(self.headings):
                return (self.headings[number - 1],)
        scored: list[tuple[float, Heading]] = []
        for heading in self.headings:
            ratio = max(
                _similarity(wanted, heading.title), _similarity(wanted, _plain(heading.title))
            )
            if ratio >= _TITLE_SIMILARITY:
                scored.append((ratio, heading))
        scored.sort(key=lambda item: -item[0])  # stable: document order among equal ratios
        return tuple(heading for _, heading in scored)

    def _extent_end(self, heading: Heading) -> int:
        """The last position of a heading's span, including the files its lines include."""
        file = self._index.files[heading.path]
        if heading.end_line < file.n_lines:
            return self.order(heading.path, heading.end_line + 1) - 1
        return file.exit - 1


def build_outline(read: Callable[[str], str], paths: Iterable[str], root_file: str) -> Outline:
    """The Outline of the document whose Root file is `root_file`.

    `paths` are the Project's files (relative, `/`-separated); a file exists
    only if it is among them. `read(path)` returns a file's text. A file that
    is missing, or whose `read` raises, is left out; an `\\input` cycle or a
    repeated `\\input` is followed once.
    """
    path_list = list(dict.fromkeys(paths))
    path_set = frozenset(path_list)
    texts: dict[str, str | None] = {}

    def load(path: str) -> str | None:
        if path not in texts:
            try:
                texts[path] = read(path).replace("\r\n", "\n")
            except Exception:  # binary, vanished or otherwise unreadable: treated as missing
                texts[path] = None
        return texts[path]

    walk = _Walk(path_set, load)
    root_text = load(root_file) if root_file in path_set else None
    if root_text is not None:
        walk.run(root_file, root_text)

    headings = walk.headings()
    index = walk.index()
    environments = sorted(
        ((off, span) for file in walk.files.values() for off, span in file.envs),
        key=lambda item: (_position(index, item[1].path, item[1].start), item[0]),
    )
    bib_keys: set[str] = set()
    for bib in walk.bib_files:
        text = load(bib) if bib in path_set else None
        if text is not None:
            bib_keys.update(_bib_entry_keys(text))
    unincluded = tuple(
        sorted(
            p
            for p in path_list
            if p.lower().endswith(".tex") and p != root_file and p not in walk.included
        )
    )
    return Outline(
        root_file=root_file,
        headings=headings,
        inputs=MappingProxyType({k: tuple(v) for k, v in walk.edges.items()}),
        labels=MappingProxyType(
            {
                key: LabelInfo(key, path, line, walk.refs[key])
                for key, (path, line) in walk.labels.items()
            }
        ),
        environments=tuple(span for _, span in environments),
        bib_files=tuple(walk.bib_files),
        bib_keys=frozenset(bib_keys),
        cited=frozenset(walk.cited),
        packages=frozenset(walk.packages),
        document_class=walk.document_class,
        unincluded=unincluded,
        digest=_digest(root_file, path_list, texts),
        _index=index,
    )


# -- scanning one file -----------------------------------------------------------

_HEADING_LEVELS: Mapping[str, int] = {
    "part": -1,
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}
_REF_COMMANDS = frozenset({"ref", "eqref", "cref", "Cref", "autoref", "pageref"})
_CITE_COMMANDS = frozenset(
    {
        "cite",
        "citep",
        "citet",
        "citealp",
        "citealt",
        "citeauthor",
        "citeyear",
        "citeyearpar",
        "Cite",
        "Citep",
        "Citet",
        "parencite",
        "Parencite",
        "textcite",
        "Textcite",
        "autocite",
        "Autocite",
        "footcite",
        "smartcite",
        "supercite",
        "fullcite",
        "nocite",
    }
)
_INPUT_COMMANDS = frozenset({"input", "include"})
_PACKAGE_COMMANDS = frozenset({"usepackage", "RequirePackage"})
# Commands after which no section body continues: a heading's span ends before them.
_BACK_MATTER_COMMANDS = frozenset(
    {"bibliographystyle", "bibliography", "printbibliography", "appendix", "backmatter"}
)
_TITLE_SIMILARITY = 0.8
_MAX_ARGUMENT = 4096  # longest braced argument the scanner reads, in characters

_CONTROL_SEQUENCE = re.compile(r"\\(?:([A-Za-z]+)|[\s\S])")
_SPECIAL = re.compile(r"[\\%]")
_VERBATIM_BEGIN = re.compile(
    r"\\begin\s*\{(verbatim\*?|Verbatim\*?|lstlisting|minted|comment|filecontents\*?)\}"
)
_VERB = re.compile(r"\\verb\*?(?![A-Za-z])")
_NOT_NEWLINE = re.compile(r"[^\n]")
_NEWLINE = re.compile(r"\n")
_LABEL_IN_TITLE = re.compile(r"\\label\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}")
_MACRO_NAME = re.compile(r"\\[A-Za-z]+\*?")
_BIB_ENTRY = re.compile(r"@\s*([A-Za-z]+)\s*[{(]\s*([^\s,{}()]+)\s*,")
_BIB_NON_ENTRIES = frozenset({"comment", "preamble", "string"})

_Kind = Literal[
    "heading", "label", "ref", "cite", "input", "begin", "end", "package", "class", "bib", "back"
]


@dataclass(frozen=True)
class _Event:
    offset: int
    line: int
    kind: _Kind
    name: str = ""  # title, label key, environment, input target or class name
    keys: tuple[str, ...] = ()  # reference, cite and package names; bib paths
    level: int = 0  # headings only
    last_line: int = 0  # headings only: the line of the title's closing brace


@dataclass(frozen=True)
class _File:
    path: str
    n_lines: int
    events: tuple[_Event, ...]  # by offset
    heading_labels: Mapping[int, str]  # event index of a heading -> its \label key
    envs: tuple[tuple[int, EnvSpan], ...]  # (offset of \begin, span)
    blank: frozenset[int]  # lines holding only whitespace
    breaks: frozenset[int]


def _mask(text: str) -> str:
    """The text with comments and verbatim bodies replaced by spaces.

    Offsets and line breaks are kept, so positions in the result are
    positions in the text. `%` starts a comment unless escaped (`\\%`). The
    bodies of verbatim-like environments and of `\\verb` are blanked; their
    `\\begin` and `\\end` stay visible.
    """
    out: list[str] = []
    n = len(text)
    i = 0
    while True:
        found = _SPECIAL.search(text, i)
        if found is None:
            out.append(text[i:])
            break
        j = found.start()
        if text[j] == "%":
            out.append(text[i:j])
            eol = text.find("\n", j)
            eol = n if eol < 0 else eol
            out.append(" " * (eol - j))
            i = eol
            continue
        env = _VERBATIM_BEGIN.match(text, j)
        if env is not None:
            out.append(text[i : env.end()])
            close = text.find(f"\\end{{{env.group(1)}}}", env.end())
            close = n if close < 0 else close
            out.append(_NOT_NEWLINE.sub(" ", text[env.end() : close]))
            i = close  # the \end is scanned as ordinary text
            continue
        verb = _VERB.match(text, j)
        if verb is not None and verb.end() < n and text[verb.end()] not in " \n":
            delim_at = verb.end()
            eol = text.find("\n", delim_at)
            eol = n if eol < 0 else eol
            close = text.find(text[delim_at], delim_at + 1, eol)
            body_end = eol if close < 0 else close
            out.append(text[i : delim_at + 1])
            out.append(" " * (body_end - delim_at - 1))
            # The closing delimiter is copied here, so a `%` delimiter is not read as a comment.
            i = body_end if close < 0 else close + 1
            if close >= 0:
                out.append(text[close])
            continue
        out.append(text[i : j + 2])  # a control symbol such as \% or \\, or a control word's start
        i = j + 2
    return "".join(out)


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i


def _braced(text: str, i: int) -> tuple[str, int] | None:
    """The content of the balanced `{...}` group at `text[i]`, and the offset after it."""
    if i >= len(text) or text[i] != "{":
        return None
    depth = 0
    j = i
    stop = min(len(text), i + _MAX_ARGUMENT)
    while j < stop:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
        j += 1
    return None


def _bracketed(text: str, i: int) -> int | None:
    """The offset after the `[...]` group at `text[i]`; a `]` inside braces does not close it."""
    if i >= len(text) or text[i] != "[":
        return None
    depth = 0
    j = i + 1
    stop = min(len(text), i + _MAX_ARGUMENT)
    while j < stop:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return None
        elif ch == "]" and depth == 0:
            return j + 1
        j += 1
    return None


def _argument(
    text: str, i: int, *, star: bool = False, optional: int = 0
) -> tuple[str, int] | None:
    """The braced argument after an optional `*` and up to `optional` `[...]` groups."""
    i = _skip_space(text, i)
    if star and i < len(text) and text[i] == "*":
        i = _skip_space(text, i + 1)
    for _ in range(optional):
        after = _bracketed(text, i)
        if after is None:
            break
        i = _skip_space(text, after)
    return _braced(text, i)


def _split_keys(value: str) -> tuple[str, ...]:
    return tuple(key for key in (part.strip() for part in value.split(",")) if key)


def _clean_title(raw: str) -> str:
    return " ".join(_LABEL_IN_TITLE.sub(" ", raw).split())


def _scan(masked: str, starts: Sequence[int]) -> list[_Event]:
    """The events of one masked file, by offset."""
    events: list[_Event] = []

    def line_of(offset: int) -> int:
        return bisect.bisect_right(starts, offset)

    for match in _CONTROL_SEQUENCE.finditer(masked):
        name = match.group(1)
        if name is None:
            continue
        at, after = match.start(), match.end()
        line = line_of(at)
        if name in _HEADING_LEVELS:
            arg = _argument(masked, after, star=True, optional=1)
            if arg is not None:
                events.append(
                    _Event(
                        at,
                        line,
                        "heading",
                        _clean_title(arg[0]),
                        level=_HEADING_LEVELS[name],
                        last_line=line_of(arg[1] - 1),
                    )
                )
        elif name == "label":
            arg = _argument(masked, after, optional=1)
            if arg is not None and arg[0].strip():
                events.append(_Event(at, line, "label", arg[0].strip()))
        elif name in _REF_COMMANDS:
            arg = _argument(masked, after, star=True)
            if arg is not None:
                events.append(_Event(at, line, "ref", keys=_split_keys(arg[0])))
        elif name in _CITE_COMMANDS:
            arg = _argument(masked, after, star=True, optional=2)
            if arg is not None:
                keys = tuple(key for key in _split_keys(arg[0]) if key != "*")
                events.append(_Event(at, line, "cite", keys=keys))
        elif name in _INPUT_COMMANDS:
            arg = _argument(masked, after)
            if arg is not None:
                events.append(_Event(at, line, "input", arg[0]))
        elif name in ("begin", "end"):
            arg = _argument(masked, after)
            if arg is not None and arg[0].strip():
                env = arg[0].strip()
                events.append(_Event(at, line, "begin" if name == "begin" else "end", env))
                if (name, env) in (("end", "document"), ("begin", "thebibliography")):
                    events.append(_Event(at, line, "back", env))
        elif name in _PACKAGE_COMMANDS:
            arg = _argument(masked, after, optional=1)
            if arg is not None:
                events.append(_Event(at, line, "package", keys=_split_keys(arg[0])))
        elif name == "documentclass":
            arg = _argument(masked, after, optional=1)
            if arg is not None and arg[0].strip():
                events.append(_Event(at, line, "class", arg[0].strip()))
        elif name == "addbibresource":
            arg = _argument(masked, after, optional=1)
            if arg is not None:
                bibs = _bib_paths(arg[0], add_suffix=False)
                events.append(_Event(at, line, "bib", keys=bibs))
        if name in _BACK_MATTER_COMMANDS:
            if name == "bibliography":
                arg = _argument(masked, after)
                if arg is not None:
                    events.append(_Event(at, line, "bib", keys=_bib_paths(arg[0], add_suffix=True)))
            events.append(_Event(at, line, "back", name))
    return events


def _parse(path: str, text: str) -> _File:
    masked = _mask(text)
    starts = [0, *(match.end() for match in _NEWLINE.finditer(text))]
    raw_lines = text.split("\n")
    masked_lines = masked.split("\n")
    n_lines = len(raw_lines) - (1 if text == "" or text.endswith("\n") else 0)
    events = _scan(masked, starts)
    blank = frozenset(n for n in range(1, n_lines + 1) if not raw_lines[n - 1].strip())

    # A \label on the heading's line, or opening the next non-blank line, belongs to it.
    heading_labels: dict[int, str] = {}
    label_lines: dict[int, int] = {}
    heading_at = [i for i, event in enumerate(events) if event.kind == "heading"]
    for k, i in enumerate(heading_at):
        heading = events[i]
        stop = heading_at[k + 1] if k + 1 < len(heading_at) else len(events)
        following = heading.last_line + 1
        while following <= n_lines and not masked_lines[following - 1].strip():
            following += 1
        opens_with_label = following <= n_lines and masked_lines[following - 1].lstrip().startswith(
            "\\label"
        )
        for event in events[i + 1 : stop]:
            if event.kind != "label":
                continue
            if event.line <= heading.last_line or (opens_with_label and event.line == following):
                heading_labels[i] = event.name
                label_lines[i] = event.line
            break

    # Environment spans: a mismatched \end closes the nearest open match; unclosed ones are dropped.
    open_envs: list[tuple[int, _Event]] = []
    spans: list[tuple[int, EnvSpan]] = []
    inline: set[int] = set()
    for i, event in enumerate(events):
        if event.kind == "begin":
            open_envs.append((i, event))
        elif event.kind == "end":
            depth = next(
                (
                    d
                    for d in range(len(open_envs) - 1, -1, -1)
                    if open_envs[d][1].name == event.name
                ),
                None,
            )
            if depth is None:
                continue
            begin_index, begin = open_envs[depth]
            del open_envs[depth:]
            spans.append((begin.offset, EnvSpan(path, begin.name, begin.line, event.line)))
            if begin.line == event.line:
                inline.update((begin_index, i))
    spans.sort(key=lambda item: item[0])

    breaks = set(blank)
    for i, event in enumerate(events):
        if event.kind == "heading":
            breaks.update(range(event.line, event.last_line + 1))
            if i in label_lines:
                breaks.add(label_lines[i])
        elif event.kind in ("input", "back"):
            breaks.add(event.line)
        elif event.kind in ("begin", "end") and i not in inline:
            breaks.add(event.line)

    return _File(
        path=path,
        n_lines=n_lines,
        events=tuple(events),
        heading_labels=MappingProxyType(heading_labels),
        envs=tuple(spans),
        blank=blank,
        breaks=frozenset(breaks),
    )


# -- following the document from the Root file -------------------------------------


def _lowest(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else min(a, b)


@dataclass
class _Frame:
    file: _File
    via: tuple[str, int] | None  # (including file, event index) of the \input that opened it
    cursor: int = 1  # the first line not yet given a position
    next_event: int = 0
    # What this file and the files it includes hold: back matter, and the highest
    # heading level (lowest number) among all headings and among those in the body.
    back: bool = False
    level_any: int | None = None
    level_body: int | None = None

    def absorb(self, other: _Frame) -> None:
        self.back |= other.back
        self.level_any = _lowest(self.level_any, other.level_any)
        self.level_body = _lowest(self.level_body, other.level_body)


@dataclass(frozen=True)
class _Found:
    path: str
    event_index: int
    event: _Event
    in_body: bool  # after \begin{document} in document order


@dataclass(frozen=True)
class _Bound:
    """Something that can end a heading's span in its file."""

    offset: int
    line: int
    level: int | None  # the highest level it brings (lowest number), if any
    back: bool


class _Walk:
    """One walk of the \\input graph from the Root file, in document order."""

    def __init__(self, paths: frozenset[str], load: Callable[[str], str | None]) -> None:
        self.paths = paths
        self.load = load
        self.files: dict[str, _File] = {}  # every file entered, in order
        self.included: set[str] = set()
        self.edges: dict[str, list[str]] = {}
        self.found: list[_Found] = []
        self.labels: dict[str, tuple[str, int]] = {}
        self.refs: Counter[str] = Counter()
        self.cited: set[str] = set()
        self.packages: set[str] = set()
        self.document_class: str | None = None
        self.bib_files: list[str] = []
        self.body_seen = False
        self.segments: dict[str, list[tuple[int, int, int]]] = {}
        self.entry: dict[str, int] = {}
        self.exit: dict[str, int] = {}
        # (including file, event index) of each followed \input -> what the included files hold
        self.followed: dict[tuple[str, int], _Frame] = {}
        self.position = 0

    def run(self, root_file: str, text: str) -> None:
        root = _parse(root_file, text)
        self.files[root_file] = root
        self.included.add(root_file)
        self.entry[root_file] = 0
        stack = [_Frame(root, via=None)]
        in_body = False
        while stack:
            frame = stack[-1]
            file = frame.file
            if frame.next_event == len(file.events):
                self._advance(frame, file.n_lines)
                self.exit[file.path] = self.position
                stack.pop()
                if frame.via is not None:
                    self.followed[frame.via] = frame
                if stack:
                    stack[-1].absorb(frame)
                continue
            index = frame.next_event
            event = file.events[index]
            frame.next_event += 1
            if event.kind == "heading":
                self.found.append(_Found(file.path, index, event, in_body))
                frame.level_any = _lowest(frame.level_any, event.level)
                if in_body:
                    frame.level_body = _lowest(frame.level_body, event.level)
            elif event.kind == "label":
                self.labels.setdefault(event.name, (file.path, event.line))
            elif event.kind == "ref":
                self.refs.update(event.keys)
            elif event.kind == "cite":
                self.cited.update(event.keys)
            elif event.kind == "package":
                self.packages.update(event.keys)
            elif event.kind == "class":
                if self.document_class is None:
                    self.document_class = event.name
            elif event.kind == "bib":
                self.bib_files.extend(b for b in event.keys if b not in self.bib_files)
            elif event.kind == "back":
                frame.back = True
            elif event.kind == "begin" and event.name == "document":
                in_body = self.body_seen = True
            elif event.kind == "input":
                child = self._enter(file.path, event)
                if child is not None:
                    self._advance(frame, event.line)
                    self.entry[child.path] = self.position
                    stack.append(_Frame(child, via=(file.path, index)))

    def _enter(self, parent: str, event: _Event) -> _File | None:
        """Record an \\input edge; the included file when it is to be followed now."""
        target = _resolve_input(event.name, self.paths)
        if target is None:
            return None
        children = self.edges.setdefault(parent, [])
        if target not in children:
            children.append(target)
        self.included.add(target)
        if target in self.files:
            return None  # a cycle or a repeat: followed once only
        text = self.load(target)
        if text is None:
            return None
        child = _parse(target, text)
        self.files[target] = child
        return child

    def _advance(self, frame: _Frame, through: int) -> None:
        """Give positions to the frame's lines up to and including `through`."""
        if through >= frame.cursor:
            self.segments.setdefault(frame.file.path, []).append(
                (frame.cursor, through, self.position)
            )
            self.position += through - frame.cursor + 1
            frame.cursor = through + 1

    def _kept(self, found: _Found) -> bool:
        # Headings before \begin{document} (in macro definitions, say) are not headings.
        return found.in_body or not self.body_seen

    def headings(self) -> tuple[Heading, ...]:
        bounds: dict[str, list[_Bound]] = {path: [] for path in self.files}
        for found in self.found:
            if self._kept(found):
                bounds[found.path].append(
                    _Bound(found.event.offset, found.event.line, found.event.level, False)
                )
        for (path, index), sub in self.followed.items():
            level = sub.level_body if self.body_seen else sub.level_any
            event = self.files[path].events[index]
            bounds[path].append(_Bound(event.offset, event.line, level, sub.back))
        for path, file in self.files.items():
            bounds[path].extend(
                _Bound(e.offset, e.line, None, True) for e in file.events if e.kind == "back"
            )
            bounds[path].sort(key=lambda bound: bound.offset)
        offsets = {path: [bound.offset for bound in listed] for path, listed in bounds.items()}

        headings: list[Heading] = []
        for found in self.found:
            if not self._kept(found):
                continue
            file = self.files[found.path]
            event = found.event
            end_line = file.n_lines
            listed = bounds[found.path]
            for k in range(bisect.bisect_right(offsets[found.path], event.offset), len(listed)):
                bound = listed[k]
                if bound.back or (bound.level is not None and bound.level <= event.level):
                    end_line = max(event.line, bound.line - 1)
                    break
            while end_line > event.line and end_line in file.blank:
                end_line -= 1
            headings.append(
                Heading(
                    path=found.path,
                    line=event.line,
                    end_line=end_line,
                    level=event.level,
                    title=event.name,
                    label=file.heading_labels.get(found.event_index),
                    index=len(headings) + 1,
                )
            )
        return tuple(headings)

    def index(self) -> _Index:
        files: dict[str, _FileIndex] = {}
        for path, file in self.files.items():
            segments = tuple(self.segments.get(path, ()))
            files[path] = _FileIndex(
                n_lines=file.n_lines,
                segments=segments,
                firsts=tuple(first for first, _, _ in segments),
                entry=self.entry[path],
                exit=self.exit[path],
                breaks=file.breaks,
            )
        return _Index(MappingProxyType(files), self.position)


# -- helpers -------------------------------------------------------------------------


def _position(index: _Index, path: str, line: int) -> int:
    file = index.files.get(path)
    if file is None:
        return index.end
    if not file.segments:
        return file.entry
    line = min(max(line, 1), file.n_lines)
    k = bisect.bisect_right(file.firsts, line) - 1
    first, _, base = file.segments[k]
    return base + line - first


def _normalize(name: str) -> str | None:
    """A project-relative path from an \\input or bibliography argument, or None."""
    name = name.strip().strip('"').strip()
    if not name or "\\" in name or name.startswith("/") or re.match(r"[A-Za-z]:", name):
        return None
    normal = posixpath.normpath(name)
    if normal in (".", "..") or normal.startswith("../"):
        return None
    return normal


def _resolve_input(argument: str, paths: frozenset[str]) -> str | None:
    """The file an `\\input{x}` names: `x.tex` when `x` has no extension."""
    name = _normalize(argument)
    if name is None:
        return None
    if PurePosixPath(name).suffix:
        candidates = (name, f"{name}.tex")
    else:
        candidates = (f"{name}.tex", name)
    return next((c for c in candidates if c in paths), None)


def _bib_paths(argument: str, *, add_suffix: bool) -> tuple[str, ...]:
    out: list[str] = []
    for part in _split_keys(argument):
        name = _normalize(part)
        if name is None:
            continue
        if add_suffix and not name.lower().endswith(".bib"):
            name = f"{name}.bib"
        out.append(name)
    return tuple(out)


def _bib_entry_keys(text: str) -> set[str]:
    return {
        match.group(2)
        for match in _BIB_ENTRY.finditer(text)
        if match.group(1).lower() not in _BIB_NON_ENTRIES
    }


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _plain(title: str) -> str:
    """A title without macro names, braces and ties, for matching a user's words."""
    text = _MACRO_NAME.sub(" ", title).replace("~", " ")
    return " ".join(text.replace("{", " ").replace("}", " ").replace("$", " ").split())


def _digest(root_file: str, paths: Sequence[str], texts: Mapping[str, str | None]) -> str:
    """sha256 over the Root file, the file list and the text of every file read."""
    digest = hashlib.sha256(b"prismtex.outline/2\n")

    def put(value: str) -> None:
        data = value.encode("utf-8", "surrogatepass")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    put(root_file)
    ordered = sorted(paths)
    digest.update(len(ordered).to_bytes(8, "big"))
    for path in ordered:
        put(path)
    for path in sorted(texts):
        put(path)
        text = texts[path]
        if text is None:
            digest.update(b"\x00")
        else:
            digest.update(b"\x01")
            put(text)
    return digest.hexdigest()


# -- the older per-file outline, served by GET /api/projects/{id}/outline -----------

_LEVELS = {
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
}

_HEADING = re.compile(
    r"\\(?P<macro>chapter|section|subsection|subsubsection|paragraph)"
    r"(?P<star>\*)?"
    r"(?:\[[^\]]*\])?"
    r"\{(?P<title>[^{}]+)\}"
)


def parse_tex_outline(text: str) -> list[dict[str, Any]]:
    """Heading dicts with title, line (1-based), level and macro (`section*` when starred)."""
    items: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        for match in _HEADING.finditer(strip_comment(raw)):
            macro = match.group("macro")
            items.append(
                {
                    "title": match.group("title").strip(),
                    "line": lineno,
                    "level": _LEVELS[macro],
                    "macro": f"{macro}{match.group('star') or ''}",
                }
            )
    return items


def outline_project(store: ProjectStore, project_id: str) -> list[dict[str, Any]]:
    """Headings of every `.tex` file: the root file first, then the rest by path."""
    root_file = store.settings(project_id).root_file
    tex_files = [e.path for e in store.list_files(project_id) if e.path.lower().endswith(".tex")]
    tex_files.sort(key=lambda path: (path != root_file, path))
    items: list[dict[str, Any]] = []
    for path in tex_files:
        try:
            text = store.read(project_id, path).text
        except AppError:
            continue  # binary, vanished or otherwise unreadable: not part of the outline
        items.extend({**item, "path": path} for item in parse_tex_outline(text))
    return items
