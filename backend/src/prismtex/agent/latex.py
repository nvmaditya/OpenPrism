"""Host-side LaTeX operations: packages, placement, bibliography and fragment cleanup.

Pure functions over Source text and the Outline (agent design §4.4). Callers
stage what they return with `by="host"`, so each change is a Hunk the user
can reject. Every operation is idempotent: run again on its own result, it
changes nothing.

Lines are 1-based. An `Edit` inserts lines after `after_line`, and 0 is the
top of the file.

All scanners are comment-aware: `%` starts a comment unless written `\\%`, so
`\\\\%` is a line break followed by a comment. The bodies of verbatim-like
environments and of `\\verb` are not read as LaTeX.

`data/packages.json` maps a macro (a key with a leading backslash) or an
environment (a bare name) to the packages that provide it. The first package
is the one to load, and any of them being loaded already is enough.
"""

from __future__ import annotations

import bisect
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Literal

from prismtex.outline import Heading, Outline

__all__ = [
    "BibError",
    "BibErrorReason",
    "BibResult",
    "Edit",
    "add_bib_entry",
    "apply_edits",
    "bib_keys",
    "cite_keys",
    "ensure_package",
    "environments_balanced",
    "insert_after_anchor",
    "insertion_point",
    "packages_needed",
    "split_usepackages",
    "strip_fragment_wrappers",
    "wire_bibliography",
]

_DEFAULT_BIB = "refs.bib"


@dataclass(frozen=True)
class Edit:
    """Lines to insert into `path` after `after_line` (0 is the top).

    An Edit with no lines carries only its note: there is nothing to stage.
    """

    path: str
    after_line: int
    lines: tuple[str, ...]  # without line terminators
    note: str  # e.g. "added \\usepackage{booktabs}"

    def __post_init__(self) -> None:
        if self.after_line < 0:
            raise ValueError("after_line is 0 or more")
        if any("\n" in line or "\r" in line for line in self.lines):
            raise ValueError("an Edit line holds no line break")


@dataclass(frozen=True)
class BibResult:
    """Where a bibliography entry is after `add_bib_entry`, and the edits that put it there."""

    key: str  # the key to cite
    path: str  # the .bib file that holds the entry
    edits: tuple[Edit, ...]  # empty when the entry was reused
    created: bool  # `path` is a new file
    reused: bool  # an identical entry was there already


BibErrorReason = Literal["parse", "duplicate_key"]


class BibError(Exception):
    """`add_bib_entry` cannot add the entry; `reason` says why and `message` says it in words."""

    reason: BibErrorReason
    message: str

    def __init__(self, reason: BibErrorReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


# -- edits ---------------------------------------------------------------------------


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    """The text with the edits applied; every `after_line` counts lines of the original text.

    Edits after the same line keep their order. All edits name one file.
    Raises ValueError for an edit past the end of the text, or for edits
    to more than one file.
    """
    if len({edit.path for edit in edits}) > 1:
        raise ValueError("the edits name more than one file")
    lines = text.split("\n")
    final_newline = text == "" or text.endswith("\n")
    if final_newline:
        lines.pop()
    inserts: dict[int, list[str]] = {}
    for edit in edits:
        if edit.after_line > len(lines):
            raise ValueError(f"line {edit.after_line} is past the end of {edit.path}")
        inserts.setdefault(edit.after_line, []).extend(edit.lines)
    if not any(inserts.values()):
        return text
    out = list(inserts.get(0, ()))
    for number, line in enumerate(lines, start=1):
        out.append(line)
        out.extend(inserts.get(number, ()))
    joined = "\n".join(out)
    return joined + "\n" if final_newline else joined


# -- packages ------------------------------------------------------------------------

_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def ensure_package(root_text: str, name: str, options: str = "", *, root_file: str) -> Edit | None:
    """An Edit that loads package `name` in the Root file, or None when it is loaded.

    None when `\\usepackage` or `\\RequirePackage` loads it already, alone or
    in a list, with every requested option. Loaded with other options,
    nothing changes: the Edit has no lines and a note that says so.
    Otherwise `\\usepackage[options]{name}` goes after the last `\\usepackage`
    line of the preamble, or after `\\documentclass`. Raises ValueError for a
    name or options that cannot be written into a `\\usepackage` line.
    """
    _check_package_request(name, options)
    wanted = _split_options(options)
    tokens = _scan(root_text)
    loads = [t for t in tokens if t.kind == "package" and name in t.keys]
    if loads:
        have = {option for t in loads for option in _split_options(t.options)}
        if set(wanted) <= have:
            return None
        first = loads[0]
        old = f"[{first.options}]" if first.options else ""
        return Edit(
            path=root_file,
            after_line=first.end_line,
            lines=(),
            note=f"kept \\{first.command}{old}{{{name}}}; requested [{','.join(wanted)}]",
        )
    line = f"\\usepackage[{','.join(wanted)}]{{{name}}}" if wanted else f"\\usepackage{{{name}}}"
    return Edit(
        path=root_file, after_line=_package_line(tokens), lines=(line,), note=f"added {line}"
    )


def split_usepackages(fragment: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The fragment without `\\usepackage` and `\\RequirePackage` commands, and what they loaded.

    Each loaded package is a (name, options) pair, with the options
    normalized as `ensure_package` writes them. A line left holding only
    spaces or a comment is dropped.
    """
    tokens = [t for t in _scan(fragment) if t.kind == "package"]
    pairs = tuple(dict.fromkeys((name, t.options) for t in tokens for name in t.keys))
    return _cut(fragment, [(t.start, t.end) for t in tokens]), pairs


def packages_needed(text: str, loaded: frozenset[str]) -> tuple[str, ...]:
    """Packages that the text's macros and environments need and `loaded` lacks.

    In order of first use. A macro that several packages provide needs none
    of them when one is loaded or is needed by another macro of the text.
    """
    table = _package_table()
    masked = _mask(text)
    used: dict[tuple[str, ...], None] = {}
    for match in _CONTROL_SEQUENCE.finditer(masked):
        command = match.group(1)
        if command is None:
            continue
        if command == "begin":
            arg = _argument(masked, match.end())
            providers = None if arg is None else table.get(arg.value.strip())
        else:
            providers = table.get(f"\\{command}")
        if providers is not None:
            used.setdefault(providers)
    only = {providers[0] for providers in used if len(providers) == 1}
    needed: dict[str, None] = {}
    for providers in used:
        first = providers[0]  # the table holds no empty list
        if not loaded.isdisjoint(providers):
            continue
        if len(providers) > 1 and not only.isdisjoint(providers):
            continue
        needed.setdefault(first)
    return tuple(needed)


@cache
def _package_table() -> Mapping[str, tuple[str, ...]]:
    resource = files("prismtex.agent").joinpath("data", "packages.json")
    raw = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("packages.json holds one object")
    table: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"packages.json: {key!r} maps to a non-empty list of package names")
        table[key] = tuple(value)
    return MappingProxyType(table)


def _check_package_request(name: str, options: str) -> None:
    if not _PACKAGE_NAME.fullmatch(name):
        raise ValueError(f"not a package name: {name!r}")
    depth = 0
    for ch in options:
        if ch in "\n\r%":
            raise ValueError("package options hold no line break and no %")
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("package options have unbalanced braces")
        elif ch == "]" and depth == 0:
            raise ValueError("package options hold no ] outside braces")
    if depth:
        raise ValueError("package options have unbalanced braces")


def _split_options(options: str) -> list[str]:
    """Options split at commas outside braces, with spaces tidied; empty ones are dropped."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(options):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(options[start:i])
            start = i + 1
    parts.append(options[start:])
    tidy = (re.sub(r"\s*=\s*", "=", " ".join(part.split())) for part in parts)
    return [part for part in tidy if part]


def _package_line(tokens: Sequence[_Token]) -> int:
    """The line a new `\\usepackage` goes after: the preamble's last one, or `\\documentclass`.

    Only commands outside braces count, so the new line never lands inside
    a conditional such as `\\IfFileExists{...}{\\usepackage{x}}{}`.
    """
    begin = _begin_document(tokens)
    preamble = _top_level_preamble(tokens, begin)
    lines = [t.end_line for t in preamble if t.kind == "package" and t.command == "usepackage"]
    lines.extend(t.end_line for t in preamble if t.kind == "class")
    if lines:
        after = max(lines)
    elif begin is not None:
        after = begin.line - 1
    else:
        after = 0
    return after if begin is None else min(after, begin.line - 1)


# -- placement -----------------------------------------------------------------------

_LABEL_ANCHOR = re.compile(r"\\label\s*\{([^{}]*)\}")


def insertion_point(
    outline: Outline, heading: Heading, *, where: Literal["start", "end"] = "end"
) -> tuple[str, int]:
    """Where new text goes in a section body: the file, and the line to insert after.

    "end" is the last line of the heading's span. The Outline ends a span
    before the next heading of the same or a higher level, following
    `\\input` files to find it, and before `\\bibliography`,
    `\\printbibliography`, `\\appendix` and `\\end{document}`, with trailing
    blank lines left out. "start" is the heading's line, or the line of its
    `\\label` when that follows it. A heading in an included file gives that
    file. Raises ValueError for a heading that is not in the Outline.
    """
    if heading not in outline.headings:
        raise ValueError("the heading is not in this Outline")
    if where == "end":
        return heading.path, heading.end_line
    if where != "start":
        raise ValueError(f"where is 'start' or 'end', not {where!r}")
    after = heading.line
    if heading.label is not None:
        info = outline.labels.get(heading.label)
        if info is not None and info.path == heading.path:
            if heading.line <= info.line <= heading.end_line:
                after = info.line
    return heading.path, after


def insert_after_anchor(text: str, anchor: str) -> int | None:
    """The line to insert after for an anchor: a `\\label` key or a unique line of text.

    A label key (bare, or written `\\label{key}`) gives the `\\end` line of
    the outermost environment around the label, leaving out `document` and
    any environment that holds a heading; with no such environment, the
    label's own line. Any other anchor names the one line that reads the
    same (spacing aside), or else the one line that contains it. None when
    nothing matches or more than one place does.
    """
    if "\n" in anchor.strip():
        return None
    wanted = " ".join(anchor.split())
    if not wanted:
        return None
    written = _LABEL_ANCHOR.fullmatch(wanted)
    key = written.group(1).strip() if written else wanted
    tokens = _scan(text)
    labels = [t for t in tokens if t.kind == "label" and t.name == key]
    if len(labels) == 1:
        return _after_environment(tokens, labels[0])
    if labels or written:
        return None
    return _unique_line(text, wanted)


def _after_environment(tokens: Sequence[_Token], label: _Token) -> int:
    headings = [t.start for t in tokens if t.kind == "heading"]
    outer: _Token | None = None
    outer_end: _Token | None = None
    for begin, end in _environment_spans(tokens):
        if begin.name == "document" or not begin.start < label.start < end.start:
            continue
        if any(begin.start < at < end.start for at in headings):
            continue
        if outer is None or begin.start < outer.start:
            outer, outer_end = begin, end
    return label.end_line if outer_end is None else outer_end.end_line


def _unique_line(text: str, wanted: str) -> int | None:
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    flat = [" ".join(line.split()) for line in lines]
    exact = [n for n, line in enumerate(flat, start=1) if line == wanted]
    if exact:
        return exact[0] if len(exact) == 1 else None
    partial = [n for n, line in enumerate(flat, start=1) if wanted in line]
    return partial[0] if len(partial) == 1 else None


def strip_fragment_wrappers(fragment: str) -> tuple[str, tuple[str, ...]]:
    """The fragment without `\\documentclass`, `\\begin{document}` and `\\end{document}`.

    For text bound for an included file or a Region. Also returns the
    removed commands as written, for the note. A line left holding only
    spaces or a comment is dropped.
    """
    tokens = [
        t
        for t in _scan(fragment)
        if t.kind == "class" or (t.kind in ("begin", "end") and t.name == "document")
    ]
    removed = tuple(fragment[t.start : t.end] for t in tokens)
    return _cut(fragment, [(t.start, t.end) for t in tokens]), removed


# -- bibliography --------------------------------------------------------------------

_BIB_MARK = re.compile(r"[%@]")
_BIB_AT = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
_BIB_KEY = re.compile(r"\s*([^\s,{}()]+)\s*,")
_BIB_NON_ENTRIES = frozenset({"comment", "preamble", "string"})
_BRACES = re.compile(r"[{}]")
_BRACES_AND_PARENS = re.compile(r"[{}()]")
_ENTRY_HEAD = re.compile(r"@\s*([A-Za-z]+)\s*(\{)\s*([^\s,{}()]+)\s*,")
_FIELD_NAME = re.compile(r"([A-Za-z][A-Za-z0-9_:.+-]*)\s*=")
_NUMBER = re.compile(r"[0-9]+")
_SYMBOL = re.compile(r"[A-Za-z][A-Za-z0-9_:.+-]*")
_BIB_PATH = re.compile(r"[^\s%#{}\\,]+\.bib", re.IGNORECASE)


def add_bib_entry(texts: Mapping[str, str], outline: Outline, entry: str) -> BibResult:
    """Add one BibTeX entry to the Project's bibliography, or find it there already.

    `texts` holds the Project's Source texts by path. The entry goes to the
    first `.bib` file the document names (with `\\bibliography` or
    `\\addbibresource`) that is among them, else to the first one it names,
    else to `refs.bib`. An entry with the same key, type and fields
    (delimiters, field order and spacing aside) is reused. Raises
    BibError("parse") when `entry` is not exactly one entry, and
    BibError("duplicate_key") when its key is taken by different fields.
    """
    new = _parse_entry(entry)
    named = [path for path in outline.bib_files if path.lower().endswith(".bib")]
    present = [path for path in named if path in texts]
    target = present[0] if present else named[0] if named else _DEFAULT_BIB
    created = target not in texts
    # The named files that exist; with none, an unnamed refs.bib that exists.
    searched: list[str] = present or ([] if created else [target])
    for path in searched:
        bib_text = texts[path]
        for span in _bib_entries(bib_text):
            if span.key.casefold() != new.key.casefold():
                continue
            old = _try_parse_entry(bib_text[span.start : span.end]) if span.closed else None
            if old is not None and (old.kind, old.fields) == (new.kind, new.fields):
                return BibResult(key=span.key, path=path, edits=(), created=False, reused=True)
            raise BibError(
                "duplicate_key",
                f"The key {span.key} is already in {path} with different fields. "
                "Cite that entry, or give the new one another key.",
            )
    current = "" if created else texts[target]
    after = _count_lines(current)
    body = tuple(new.text.split("\n"))
    lines = ("", *body) if after and _last_line(current).strip() else body
    edit = Edit(
        path=target,
        after_line=after,
        lines=lines,
        note=f"added bibliography entry {new.key} to {target}",
    )
    return BibResult(key=new.key, path=target, edits=(edit,), created=created, reused=False)


def wire_bibliography(
    root_text: str, outline: Outline, bib_path: str, *, root_file: str
) -> tuple[Edit, ...]:
    """Edits to the Root file that make the document read `bib_path`; none when it does.

    Nothing changes when the document names `bib_path` already. With biblatex
    loaded, `\\addbibresource{<bib_path>}` goes into the preamble, and
    `\\printbibliography` before `\\end{document}` unless the Root file has
    one or the document names other resources. Without biblatex,
    `\\bibliographystyle{plain}` (unless a style is set) and
    `\\bibliography{<bib_path without .bib>}` go before `\\end{document}`,
    unless the document has a bibliography already. Raises ValueError for
    a path that is not a `.bib` file name TeX can read.
    """
    if not _BIB_PATH.fullmatch(bib_path):
        raise ValueError(f"not a .bib path for the document: {bib_path!r}")
    if bib_path in outline.bib_files:
        return ()
    tokens = _scan(root_text)
    begin = _begin_document(tokens)
    end = next((t for t in tokens if t.kind == "end" and t.name == "document"), None)
    before_end: int | None = None
    if end is not None and (begin is None or end.line > begin.line):
        before_end = end.line - 1
    printed = any(t.kind in ("bibliography", "printbibliography") for t in tokens)
    biblatex = "biblatex" in outline.packages or any(
        t.kind == "package" and "biblatex" in t.keys for t in tokens
    )
    if biblatex:
        edits: list[Edit] = []
        resource_line = _resource_line(tokens, begin)
        if resource_line is not None:
            line = f"\\addbibresource{{{bib_path}}}"
            edits.append(Edit(root_file, resource_line, (line,), f"added {line}"))
        if before_end is not None and not printed and not outline.bib_files:
            line = "\\printbibliography"
            edits.append(Edit(root_file, before_end, (line,), f"added {line}"))
        return tuple(edits)
    if before_end is None or printed or outline.bib_files:
        return ()
    lines: list[str] = []
    if not any(t.kind == "bibliographystyle" for t in tokens):
        lines.append("\\bibliographystyle{plain}")
    lines.append(f"\\bibliography{{{bib_path[: -len('.bib')]}}}")
    return (Edit(root_file, before_end, tuple(lines), "added " + " and ".join(lines)),)


def bib_keys(bib_text: str) -> tuple[str, ...]:
    """The entry keys of a `.bib` text in file order, each once.

    `@string`, `@preamble` and `@comment` are not entries. Outside an entry
    `%` comments out the rest of the line; inside one it is text.
    """
    return tuple(dict.fromkeys(span.key for span in _bib_entries(bib_text)))


def cite_keys(tex_text: str) -> tuple[str, ...]:
    """The keys the text's citation commands name, in order of first use, each once."""
    keys: dict[str, None] = {}
    for token in _scan(tex_text):
        if token.kind == "cite":
            for key in token.keys:
                keys.setdefault(key)
    return tuple(keys)


@dataclass(frozen=True)
class _BibSpan:
    kind: str  # the entry type, lowercased
    key: str
    start: int  # offset of the @
    end: int  # offset after the closing delimiter, or the end of the text when unclosed
    closed: bool


@dataclass(frozen=True)
class _Entry:
    kind: str  # lowercased
    key: str
    fields: Mapping[str, str]  # lowercased name -> normalized value
    text: str  # the entry as given, stripped, with LF line breaks


def _bib_entries(text: str) -> Iterator[_BibSpan]:
    i = 0
    n = len(text)
    while True:
        mark = _BIB_MARK.search(text, i)
        if mark is None:
            return
        i = mark.start()
        if text[i] == "%":
            eol = text.find("\n", i)
            i = n if eol < 0 else eol + 1
            continue
        head = _BIB_AT.match(text, i)
        if head is None:
            i += 1
            continue
        kind = head.group(1).lower()
        close = _bib_close(text, head.start(2))
        key = None if kind in _BIB_NON_ENTRIES else _BIB_KEY.match(text, head.end())
        if key is not None:
            end = n if close is None else close + 1
            yield _BibSpan(kind, key.group(1), i, end, close is not None)
        # An unclosed entry swallows nothing: the scan goes on after its header.
        i = head.end() if close is None else close + 1


def _bib_close(text: str, at: int) -> int | None:
    """The offset of the delimiter closing the `{` or `(` at `text[at]`.

    Braces always count, as they do for BibTeX: a backslash does not
    escape one.
    """
    if text[at] == "{":
        depth = 1
        for match in _BRACES.finditer(text, at + 1):
            depth += 1 if match.group() == "{" else -1
            if depth == 0:
                return match.start()
        return None
    depth = 0
    for match in _BRACES_AND_PARENS.finditer(text, at + 1):
        ch = match.group()
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return None
        elif ch == ")" and depth == 0:
            return match.start()
    return None


def _parse_entry(entry: str) -> _Entry:
    text = entry.replace("\r\n", "\n").replace("\r", "\n").strip()
    head = _ENTRY_HEAD.match(text)
    if head is None:
        raise BibError("parse", "A BibTeX entry starts with @type{key, with a comma after the key.")
    kind = head.group(1).lower()
    if kind in _BIB_NON_ENTRIES:
        raise BibError("parse", f"@{head.group(1)} is not a bibliography entry.")
    close = _bib_close(text, head.start(2))
    if close is None:
        raise BibError("parse", "The braces of the entry are not balanced.")
    if text[close + 1 :].strip():
        raise BibError("parse", "Give exactly one entry: there is text after its closing brace.")
    fields = _parse_fields(text, head.end(), close)
    return _Entry(kind, head.group(3), MappingProxyType(fields), text)


def _try_parse_entry(entry: str) -> _Entry | None:
    try:
        return _parse_entry(entry)
    except BibError:
        return None


def _parse_fields(text: str, i: int, stop: int) -> dict[str, str]:
    """The `name = value` fields between offset `i` and the entry's closing brace at `stop`."""
    fields: dict[str, str] = {}
    while True:
        i = _skip_space(text, i)
        if i >= stop:
            return fields
        name = _FIELD_NAME.match(text, i, stop)
        if name is None:
            raise BibError("parse", "Expected a field such as title = {...}.")
        field = name.group(1).lower()
        if field in fields:
            raise BibError("parse", f"The field {field} appears twice.")
        fields[field], i = _parse_value(text, name.end(), stop)
        i = _skip_space(text, i)
        if i >= stop:
            return fields
        if text[i] != ",":
            raise BibError("parse", f"Expected a comma after the field {field}.")
        i += 1


def _parse_value(text: str, i: int, stop: int) -> tuple[str, int]:
    """One field value, normalized for comparison, and the offset after it.

    `{...}`, `"..."` and a number compare equal when their text is the same;
    an `@string` name compares by name, ignoring case.
    """
    parts: list[str] = []
    while True:
        i = _skip_space(text, i)
        if i >= stop:
            raise BibError("parse", "A field has no value.")
        ch = text[i]
        if ch == "{":
            close = _bib_close(text, i)
            if close is None or close >= stop:
                raise BibError("parse", "The braces of a field value are not balanced.")
            parts.append("{" + " ".join(text[i + 1 : close].split()) + "}")
            i = close + 1
        elif ch == '"':
            close = _quote_close(text, i, stop)
            if close is None:
                raise BibError("parse", "A quoted field value is not closed.")
            parts.append("{" + " ".join(text[i + 1 : close].split()) + "}")
            i = close + 1
        elif (number := _NUMBER.match(text, i, stop)) is not None:
            parts.append("{" + number.group() + "}")
            i = number.end()
        elif (symbol := _SYMBOL.match(text, i, stop)) is not None:
            parts.append(symbol.group().lower())
            i = symbol.end()
        else:
            raise BibError("parse", 'A field value is {...}, "...", a number or a string name.')
        i = _skip_space(text, i)
        if i < stop and text[i] == "#":
            i += 1
            continue
        return " # ".join(parts), i


def _quote_close(text: str, i: int, stop: int) -> int | None:
    depth = 0
    for j in range(i + 1, stop):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return None
        elif ch == '"' and depth == 0:
            return j
    return None


def _resource_line(tokens: Sequence[_Token], begin: _Token | None) -> int | None:
    """The line `\\addbibresource` goes after: the last one, biblatex's, or the preamble's end."""
    preamble = _top_level_preamble(tokens, begin)
    resources = [t.end_line for t in preamble if t.kind == "addbibresource"]
    loads = [t.end_line for t in preamble if t.kind == "package" and "biblatex" in t.keys]
    if resources:
        after = resources[-1]
    elif loads:
        after = loads[-1]
    elif begin is not None:
        after = begin.line - 1
    else:
        return None
    return after if begin is None else min(after, begin.line - 1)


# -- environments --------------------------------------------------------------------


def environments_balanced(text: str) -> bool:
    """True when every `\\begin{x}` is closed by its own `\\end{x}`, properly nested."""
    stack: list[str] = []
    for token in _scan(text):
        if token.kind == "begin":
            stack.append(token.name)
        elif token.kind == "end":
            if not stack or stack[-1] != token.name:
                return False
            stack.pop()
    return not stack


def _environment_spans(tokens: Sequence[_Token]) -> list[tuple[_Token, _Token]]:
    """(begin, end) pairs; a mismatched `\\end` closes the nearest open match, as in the Outline."""
    opened: list[_Token] = []
    spans: list[tuple[_Token, _Token]] = []
    for token in tokens:
        if token.kind == "begin":
            opened.append(token)
        elif token.kind == "end":
            for depth in range(len(opened) - 1, -1, -1):
                if opened[depth].name == token.name:
                    spans.append((opened[depth], token))
                    del opened[depth:]
                    break
    return spans


def _begin_document(tokens: Sequence[_Token]) -> _Token | None:
    return next((t for t in tokens if t.kind == "begin" and t.name == "document"), None)


def _top_level_preamble(tokens: Sequence[_Token], begin: _Token | None) -> list[_Token]:
    """The commands before `\\begin{document}` that no brace group holds."""
    return [t for t in tokens if t.depth == 0 and (begin is None or t.start < begin.start)]


# -- scanning ------------------------------------------------------------------------

_SPECIAL = re.compile(r"[\\%]")
_VERBATIM_BEGIN = re.compile(
    r"\\begin\s*\{(verbatim\*?|Verbatim\*?|lstlisting|minted|comment|filecontents\*?)\}"
)
_VERB = re.compile(r"\\verb\*?(?![A-Za-z])")
_NOT_NEWLINE = re.compile(r"[^\n]")
_NEWLINE = re.compile(r"\n")
_GROUPING = re.compile(r"\\[\s\S]|[{}]")
_CONTROL_SEQUENCE = re.compile(r"\\(?:([A-Za-z]+)|[\s\S])")
_MAX_ARGUMENT = 4096  # longest braced argument the scanner reads, in characters

_PACKAGE_COMMANDS = frozenset({"usepackage", "RequirePackage"})
_HEADING_COMMANDS = frozenset(
    {"part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"}
)
_CITE_COMMANDS = frozenset(
    {
        "cite",
        "Cite",
        "citep",
        "Citep",
        "citet",
        "Citet",
        "citealp",
        "Citealp",
        "citealt",
        "Citealt",
        "citeauthor",
        "Citeauthor",
        "citeyear",
        "citeyearpar",
        "citenum",
        "citetitle",
        "citedate",
        "citeurl",
        "parencite",
        "Parencite",
        "textcite",
        "Textcite",
        "autocite",
        "Autocite",
        "footcite",
        "Footcite",
        "footcitetext",
        "smartcite",
        "Smartcite",
        "supercite",
        "fullcite",
        "footfullcite",
        "nocite",
    }
)


@dataclass(frozen=True)
class _Token:
    """One command the scanner reads, with its arguments."""

    kind: str  # package, class, begin, end, label, cite, heading, or a bibliography command
    start: int  # offset of the backslash
    end: int  # offset after the command's last argument
    line: int
    end_line: int  # the line of the command's last character
    depth: int  # how many braces are open where the command starts; 0 is the top level
    command: str  # the control word, such as "usepackage" or "RequirePackage"
    name: str = ""  # environment, label key or document class
    keys: tuple[str, ...] = ()  # package names, cite keys or bibliography resources
    options: str = ""  # package options, normalized


@dataclass(frozen=True)
class _Arg:
    optional: tuple[str, ...]  # the [...] groups read before the braced argument
    value: str
    end: int  # offset after the closing brace


def _scan(text: str) -> tuple[_Token, ...]:
    """The commands of the text that these operations read, in order, outside comments."""
    masked = _mask(text)
    starts = [0, *(match.end() for match in _NEWLINE.finditer(text))]
    braces: list[int] = []  # offsets of the braces that open or close a group
    depths: list[int] = []  # the depth after each of them
    depth = 0
    for match in _GROUPING.finditer(masked):
        if match.group() == "{":
            depth += 1
        elif match.group() == "}":
            depth = max(0, depth - 1)
        else:
            continue  # an escaped character such as \{
        braces.append(match.start())
        depths.append(depth)
    tokens: list[_Token] = []

    def add(
        kind: str,
        at: int,
        end: int,
        command: str,
        *,
        name: str = "",
        keys: tuple[str, ...] = (),
        options: str = "",
    ) -> None:
        line = bisect.bisect_right(starts, at)
        end_line = bisect.bisect_right(starts, max(at, end - 1))
        k = bisect.bisect_left(braces, at) - 1
        level = depths[k] if k >= 0 else 0
        tokens.append(_Token(kind, at, end, line, end_line, level, command, name, keys, options))

    for match in _CONTROL_SEQUENCE.finditer(masked):
        command = match.group(1)
        if command is None:
            continue
        at, after = match.start(), match.end()
        if command in _PACKAGE_COMMANDS:
            arg = _argument(masked, after, optional=1)
            if arg is not None:
                date = _bracketed(masked, arg.end)
                options = ",".join(_split_options(arg.optional[0])) if arg.optional else ""
                end = arg.end if date is None else date[1]
                add("package", at, end, command, keys=_split_list(arg.value), options=options)
        elif command == "documentclass":
            arg = _argument(masked, after, optional=1)
            if arg is not None:
                add("class", at, arg.end, command, name=arg.value.strip())
        elif command in ("begin", "end", "label"):
            arg = _argument(masked, after, optional=1 if command == "label" else 0)
            if arg is not None and arg.value.strip():
                add(command, at, arg.end, command, name=arg.value.strip())
        elif command in _CITE_COMMANDS:
            arg = _argument(masked, after, star=True, optional=2)
            if arg is not None:
                keys = tuple(key for key in _split_list(arg.value) if key != "*")
                add("cite", at, arg.end, command, keys=keys)
        elif command in _HEADING_COMMANDS:
            arg = _argument(masked, after, star=True, optional=1)
            if arg is not None:
                add("heading", at, arg.end, command)
        elif command in ("bibliography", "bibliographystyle", "addbibresource"):
            arg = _argument(masked, after, optional=1 if command == "addbibresource" else 0)
            if arg is not None:
                add(command, at, arg.end, command, keys=_split_list(arg.value))
        elif command == "printbibliography":
            add(command, at, after, command)
    return tuple(tokens)


def _mask(text: str) -> str:
    """The text with comments and verbatim bodies replaced by spaces.

    Offsets and line breaks are kept, so positions in the result are
    positions in the text. The rules are the Outline's: `%` starts a comment
    unless escaped, and `\\begin`/`\\end` of a verbatim-like environment stay
    visible while its body is blanked.
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
            i = close
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
            i = body_end if close < 0 else close + 1
            if close >= 0:
                out.append(text[close])
            continue
        out.append(text[i : j + 2])  # a control symbol such as \% or \\, or a control word's start
        i = j + 2
    return "".join(out)


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
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


def _bracketed(text: str, i: int) -> tuple[str, int] | None:
    """The content of the `[...]` group at `text[i]`, and the offset after it.

    A `]` inside braces does not close the group.
    """
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
            return text[i + 1 : j], j + 1
        j += 1
    return None


def _argument(text: str, i: int, *, star: bool = False, optional: int = 0) -> _Arg | None:
    """The braced argument after an optional `*` and up to `optional` `[...]` groups."""
    i = _skip_space(text, i)
    if star and i < len(text) and text[i] == "*":
        i = _skip_space(text, i + 1)
    groups: list[str] = []
    for _ in range(optional):
        group = _bracketed(text, i)
        if group is None:
            break
        groups.append(group[0])
        i = _skip_space(text, group[1])
    braced = _braced(text, i)
    if braced is None:
        return None
    return _Arg(tuple(groups), braced[0], braced[1])


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(item for item in (part.strip() for part in value.split(",")) if item)


def _cut(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """The text without the spans; a line they leave holding only spaces or a comment is dropped."""
    if not spans:
        return text
    masked = _mask(text)
    cut = bytearray(len(text))
    for start, end in spans:
        cut[start:end] = b"\x01" * (end - start)
    kept: list[str] = []
    offset = 0
    for raw in text.split("\n"):
        start, end = offset, offset + len(raw)
        offset = end + 1
        if not any(cut[start:end]):
            kept.append(raw)
            continue
        if "".join(masked[k] for k in range(start, end) if not cut[k]).strip():
            kept.append("".join(text[k] for k in range(start, end) if not cut[k]))
    return "\n".join(kept)


def _count_lines(text: str) -> int:
    return text.count("\n") + (0 if text == "" or text.endswith("\n") else 1)


def _last_line(text: str) -> str:
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines[-1] if lines else ""
