"""Turn TeX, BibTeX and latexmk output into Diagnostics (pure functions).

The TeX log is read with `max_print_line=10000`, so lines are not wrapped.
The current file is tracked through the `(` and `)` that TeX prints when it
opens and closes a file. Source text that TeX echoes (error context, runaway
arguments, box contents) can hold unbalanced parentheses, so those lines are
skipped by the file tracker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from prismtex.errors import PathPolicyError
from prismtex.workspace import check_relative_path

__all__ = [
    "MAX_DIAGNOSTICS",
    "Diagnostic",
    "Severity",
    "combine_diagnostics",
    "parse_blg",
    "parse_log",
    "parse_tool_output",
]

Severity = Literal["error", "warning", "info"]

MAX_DIAGNOSTICS = 200


@dataclass(frozen=True)
class Diagnostic:
    file: str | None  # project-relative POSIX path, or None outside the project
    line: int | None  # 1-based; None when unknown or when `file` is None
    severity: Severity
    kind: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
        }


# -- paths -------------------------------------------------------------------

# Files TeX and BibTeX write themselves: a location in one is not a place
# the user can edit.
_BUILD_SUFFIXES = frozenset(
    {".aux", ".bbl", ".bcf", ".blg", ".fls", ".log", ".lof", ".lot", ".nav", ".out", ".snm"}
    | {".toc", ".vrb", ".xml"}
)
# Extensions that make a bare name (no ./ or /) count as a file name.
_TEX_EXTENSIONS = frozenset(
    {"tex", "ltx", "sty", "cls", "clo", "cfg", "def", "fd", "dtx", "bib", "bst", "bbx", "cbx"}
    | {"lbx", "ldf", "dfu", "lua", "aux", "bbl", "toc", "lof", "lot", "out", "nav", "snm", "ind"}
    | {"tikz", "pgf", "mkii", "txt"}
)
_DRIVE = re.compile(r"^[A-Za-z]:/")
_PATH_START = re.compile(r"^(?:\.{1,2}[/\\]|/|~[/\\]|[A-Za-z]:[/\\]|\\\\)")
_BARE_NAME = re.compile(r"^[\w.\-/\\]+$")
# TeX prints names with spaces unquoted, so a name runs to the first
# extension that is followed by a space, `)` or the end of the line.
_SPACED_PATH = re.compile(r"(?:\.{1,2}/|/|~/|[A-Za-z]:[/\\])[^()\"]*?\.[A-Za-z0-9]+(?=[\s)]|$)")
_TOKEN = re.compile(r"[^\s()\"]+")


def _looks_like_path(token: str) -> bool:
    if _PATH_START.match(token):
        return True
    if token.startswith('"') and token.endswith('"') and len(token) > 2:
        return True
    _, dot, ext = token.rpartition(".")
    return bool(dot) and ext.lower() in _TEX_EXTENSIONS and bool(_BARE_NAME.match(token))


def _project_path(raw: str, src: str | None) -> str | None:
    """Map a path as TeX printed it to a project-relative path, or None."""
    path = raw.strip().strip('"').replace("\\", "/")
    if path.startswith("./"):
        rel = path
        while rel.startswith("./"):
            rel = rel[2:]
    elif path.startswith("/") or _DRIVE.match(path):
        if src is None:
            return None
        prefix = src.rstrip("/") + "/"
        folded = _DRIVE.match(prefix) is not None
        if not (path.lower() if folded else path).startswith(prefix.lower() if folded else prefix):
            return None
        rel = path[len(prefix) :]
    elif path.startswith("~"):
        return None
    else:
        # A bare relative name, as BibTeX prints them (and MiKTeX may).
        rel = path
    try:
        rel = check_relative_path(rel)
    except PathPolicyError:
        return None
    if PurePosixPath(rel).suffix.lower() in _BUILD_SUFFIXES:
        return None
    return rel


# -- TeX log -----------------------------------------------------------------

_FILE_LINE_ERROR = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<msg>.*)$")
_BANG_ERROR = re.compile(r"^! (?P<msg>.*)$")
_CONTEXT = re.compile(r"^l\.(?P<line>\d+)(?: |$)")
_RUNAWAY = re.compile(r"^Runaway (?:argument|definition|preamble|text)\?$")
_BAD_BOX = re.compile(r"^(?:Overfull|Underfull) \\[hv]box \(")
_BOX_LINE = re.compile(r"\bat lines? (\d+)")
_JOB_ABORTED = re.compile(r"^\*\*\* \((?P<msg>job aborted[^)]*)\)")
_WARNING = re.compile(
    r"(?P<prefix>LaTeX Font Warning|LaTeX Warning|(?:Package|Class) (?P<name>[^\s:]+) Warning): "
)
_CONTINUATION = re.compile(r"^\((?P<name>[^()\s]+)\)\s+(?P<rest>.*)$")
_INPUT_LINE = re.compile(r"on input line (\d+)")
_CITATION = re.compile(r"Warning: Citation [`'][^']*' (?:on page \S+ )?undefined")
_REFERENCE = re.compile(r"Warning: (?:Hyper )?[Rr]eference [`'][^']*' (?:on page \S+ )?undefined")
_MULTIPLY_DEFINED = re.compile(r"Warning: Label [`'][^']*' multiply defined")
_PACKAGE_ERROR = re.compile(r"^(?:Package|Class) (?P<name>[^\s:]+) Error: ")
_FILE_NOT_FOUND = re.compile(r"File [`'][^']*' not found|^I can't find file")
_FATAL = re.compile(r"^(?:Emergency stop|==> Fatal error occurred)")

# How far to look for an error's `l.<n>` context line, and the most lines of
# help text TeX prints after it.
_CONTEXT_WINDOW = 40
_HELP_WINDOW = 20


def _error_kind(message: str) -> str:
    if message.startswith("Undefined control sequence"):
        return "undefined_control_sequence"
    if _FATAL.match(message):
        return "fatal"
    if _FILE_NOT_FOUND.search(message) and (
        message.startswith(("LaTeX Error:", "I can't find file")) or _PACKAGE_ERROR.match(message)
    ):
        return "missing_file"
    if message.startswith("LaTeX Error:"):
        return "latex_error"
    if _PACKAGE_ERROR.match(message):
        return "package_error"
    return "tex_error"


def _warning_kind(text: str, prefix: str) -> str:
    if _CITATION.search(text):
        return "undefined_citation"
    if _REFERENCE.search(text):
        return "undefined_reference"
    if _MULTIPLY_DEFINED.search(text):
        return "multiply_defined_label"
    if prefix == "LaTeX Font Warning":
        return "font_warning"
    if prefix == "LaTeX Warning":
        return "latex_warning"
    return "package_warning"


@dataclass(frozen=True)
class _Header:
    file: str | None  # raw, as printed; None for `! ...` errors
    line: int | None
    message: str


class _TexLog:
    def __init__(self, lines: list[str], src: str | None) -> None:
        self.lines = lines
        self.src = src
        self.stack: list[str | None] = []  # raw file names; None for a non-file `(`
        self.found: list[Diagnostic] = []

    def parse(self) -> list[Diagnostic]:
        index = 0
        while index < len(self.lines):
            index = self._step(index)
        return self.found

    # -- dispatch

    def _step(self, index: int) -> int:
        line = self.lines[index]
        header = self._header(line)
        if header is not None:
            return self._error(index, header)
        if _RUNAWAY.match(line):
            return index + 2  # the next line is the runaway source text
        if _BAD_BOX.match(line):
            return self._bad_box(index)
        if aborted := _JOB_ABORTED.match(line):
            self._add(self._current(), None, "error", "fatal", aborted["msg"])
            return index + 1
        if warning := _WARNING.search(line):
            self._scan(line[: warning.start()])
            return self._warning(index, warning)
        self._scan(line)
        return index + 1

    def _header(self, line: str) -> _Header | None:
        if match := _FILE_LINE_ERROR.match(line):
            if _looks_like_path(match["file"]):
                return _Header(match["file"], int(match["line"]), match["msg"].strip())
        if match := _BANG_ERROR.match(line):
            return _Header(None, None, match["msg"].strip())
        return None

    # -- the file stack

    def _scan(self, text: str) -> None:
        pos = 0
        while pos < len(text):
            char = text[pos]
            if char == "(":
                name, pos = self._read_name(text, pos + 1)
                self.stack.append(name)
            elif char == ")":
                if self.stack:
                    self.stack.pop()
                pos += 1
            else:
                pos += 1

    @staticmethod
    def _read_name(text: str, start: int) -> tuple[str | None, int]:
        """The file name right after a `(`, and where scanning resumes."""
        if text.startswith('"', start):
            end = text.find('"', start + 1)
            if end > start + 1:
                return text[start + 1 : end], end + 1
            return None, start
        if spaced := _SPACED_PATH.match(text, start):
            return spaced.group(0), spaced.end()
        token = _TOKEN.match(text, start)
        if token and _looks_like_path(token.group(0)):
            return token.group(0), token.end()
        return None, start

    def _current(self) -> str | None:
        for name in reversed(self.stack):
            if name is not None:
                return _project_path(name, self.src)
        return None

    # -- messages

    def _add(
        self, file: str | None, line: int | None, severity: Severity, kind: str, message: str
    ) -> None:
        # A line number means nothing without the file it belongs to.
        self.found.append(Diagnostic(file, line if file else None, severity, kind, message))

    def _error(self, index: int, header: _Header) -> int:
        lines = self.lines
        file = _project_path(header.file, self.src) if header.file else self._current()
        line = header.line
        message = header.message

        after = index + 1
        if package := _PACKAGE_ERROR.match(message):
            while after < len(lines) and (cont := _CONTINUATION.match(lines[after])):
                if cont["name"] != package["name"]:
                    break
                message += " " + cont["rest"].strip()
                after += 1

        # TeX prints context lines, then `l.<n> <source up to the error>`,
        # then the rest of that source line, then help text up to a blank line.
        context: int | None = None
        cursor = after
        while cursor < min(len(lines), after + _CONTEXT_WINDOW):
            if _CONTEXT.match(lines[cursor]):
                context = cursor
                break
            following = self._header(lines[cursor])
            if following is not None:
                # A `! LaTeX Error: File ... not found.` has no context of its
                # own; the Emergency stop right after it carries the line.
                same_file = following.file and _project_path(following.file, self.src) == file
                if line is None and same_file:
                    line = following.line
                break
            cursor += 1

        if context is not None:
            if line is None:
                match = _CONTEXT.match(lines[context])
                assert match is not None
                line = int(match["line"])
            message += "\n" + lines[context].rstrip()
        self._add(file, line, "error", _error_kind(message), message)

        if context is not None:
            return self._skip_to_blank(context + 2, context + 2 + _HELP_WINDOW)
        return self._skip_to_blank(after, after + _HELP_WINDOW)

    def _skip_to_blank(self, start: int, limit: int) -> int:
        """Skip echoed lines up to the next empty line, `limit` or error header.

        Records a `*** (job aborted ...)` line found on the way.
        """
        cursor = start
        while cursor < min(len(self.lines), limit):
            text = self.lines[cursor]
            if text == "" or self._header(text) is not None:
                break
            if aborted := _JOB_ABORTED.match(text):
                self._add(self._current(), None, "error", "fatal", aborted["msg"])
            cursor += 1
        return cursor

    def _warning(self, index: int, match: re.Match[str]) -> int:
        lines = self.lines
        text = lines[index][match.start() :].rstrip()
        prefix = match["prefix"]
        # \PackageWarning continues lines with `(<package>)`, font warnings
        # with `(Font)`, and LaTeX's own warnings with spaces.
        own = match["name"] or ("Font" if prefix == "LaTeX Font Warning" else "LaTeX")
        cursor = index + 1
        while cursor < len(lines):
            following = lines[cursor]
            if (cont := _CONTINUATION.match(following)) and cont["name"] == own:
                text += " " + cont["rest"].strip()
            elif following.startswith("  ") and following.strip():
                text += " " + following.strip()
            else:
                break
            cursor += 1
        numbers = _INPUT_LINE.findall(text)
        line = int(numbers[-1]) if numbers else None
        self._add(self._current(), line, "warning", _warning_kind(text, prefix), text)
        return cursor

    def _bad_box(self, index: int) -> int:
        text = self.lines[index].strip()
        number = _BOX_LINE.search(text)
        self._add(self._current(), int(number[1]) if number else None, "info", "bad_box", text)
        # The box contents follow, up to a blank line.
        return self._skip_to_blank(index + 1, index + 1 + _HELP_WINDOW)


def parse_log(text: str, *, src_dir: Path) -> list[Diagnostic]:
    """Diagnostics from a TeX `.log`, with paths relative to `src_dir`."""
    found = _TexLog(text.splitlines(), Path(src_dir).as_posix()).parse()
    return _finish(found)


# -- BibTeX and Biber --------------------------------------------------------

_BLG_LOCATION = re.compile(r"line (?P<line>\d+) of file (?P<file>.+?)\s*$")
_BLG_READING = re.compile(r"while reading file (?P<file>.+?)\s*$")
_BLG_WARNING_LOCATION = re.compile(r"^--line (?P<line>\d+) of file (?P<file>.+?)\s*$")
_BIBER = re.compile(r"^\[\d+\] [^>]*> (?P<level>WARN|ERROR) - (?P<msg>.*)$")


def _blg_path(raw: str, src: str | None) -> str | None:
    path = _project_path(raw, src)
    # A location in a style file is in BibTeX's own code, not the user's data.
    if path is None or path.lower().endswith(".bst"):
        return None
    return path


def parse_blg(text: str, *, src_dir: Path | None = None) -> list[Diagnostic]:
    """Diagnostics from a BibTeX `.blg` (or a Biber one)."""
    src = Path(src_dir).as_posix() if src_dir is not None else None
    lines = text.splitlines()
    found: list[Diagnostic] = []
    for index, line in enumerate(lines):
        if biber := _BIBER.match(line):
            warn = biber["level"] == "WARN"
            found.append(
                Diagnostic(
                    None,
                    None,
                    "warning" if warn else "error",
                    "bibtex_warning" if warn else "bibtex_error",
                    biber["msg"].strip(),
                )
            )
            continue
        if line.startswith("Warning--"):
            file = number = None
            if index + 1 < len(lines) and (loc := _BLG_WARNING_LOCATION.match(lines[index + 1])):
                file, number = _blg_path(loc["file"], src), int(loc["line"])
            message = line.removeprefix("Warning--").strip()
            found.append(
                Diagnostic(file, number if file else None, "warning", "bibtex_warning", message)
            )
            continue
        if "---" not in line or line.startswith((" :", "--line")):
            continue
        head, _, tail = line.partition("---")
        head = head.strip()
        if head in ("", "while executing"):
            # The message is the line before; this line only says where.
            previous = lines[index - 1].strip() if index > 0 else ""
            message = previous or line.strip()
        elif _BLG_LOCATION.match(tail) or _BLG_READING.match(tail):
            message = head
        else:
            message = line.strip()
        file = number = None
        if loc := _BLG_LOCATION.match(tail):
            file, number = _blg_path(loc["file"], src), int(loc["line"])
        elif reading := _BLG_READING.match(tail):
            file = _blg_path(reading["file"], src)
        found.append(Diagnostic(file, number if file else None, "error", "bibtex_error", message))
    return _finish(found)


# -- latexmk's own output ------------------------------------------------------

_DENIED = re.compile(
    r"^(?:(?P<program>[\w.-]+): )?Not (?P<action>reading from|writing to) (?P<path>.+?) "
    r"\((?:openin_any|openout_any) = \w\)\.?\s*$"
)


def parse_tool_output(text: str) -> list[Diagnostic]:
    """Diagnostics from latexmk's output: file access the hardening refused."""
    found: list[Diagnostic] = []
    for line in text.splitlines():
        if denied := _DENIED.match(line.strip()):
            program = denied["program"] or "TeX"
            if denied["action"] == "reading from":
                message = (
                    f"{program} was not allowed to read '{denied['path']}': "
                    "a project can only read files inside its own folder."
                )
            else:
                message = (
                    f"{program} was not allowed to write '{denied['path']}': "
                    "a project can only write to its build folder."
                )
            found.append(Diagnostic(None, None, "warning", "access_denied", message))
    return _finish(found)


# -- shared --------------------------------------------------------------------

_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


def combine_diagnostics(*groups: Iterable[Diagnostic]) -> list[Diagnostic]:
    """Several parsers' diagnostics as one list, deduplicated and capped."""
    return _finish(item for group in groups for item in group)


def _finish(found: Iterable[Diagnostic]) -> list[Diagnostic]:
    """Drop duplicates, then keep at most MAX_DIAGNOSTICS, errors first.

    The kept diagnostics stay in log order.
    """
    unique = list(dict.fromkeys(found))
    if len(unique) <= MAX_DIAGNOSTICS:
        return unique
    ranked = sorted(range(len(unique)), key=lambda i: (_SEVERITY_RANK[unique[i].severity], i))
    return [unique[i] for i in sorted(ranked[:MAX_DIAGNOSTICS])]
