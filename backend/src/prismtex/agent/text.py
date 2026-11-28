"""Pure text functions behind the agent's edit tools.

Numbered views, the str_replace matching ladder, closest-match windows and the
escape guard (agent design §2.2 `text`, §4.5 and §4.6). Every function works on
LF text and returns a value; none of them reads or writes Project files. The
only data they load are the two word lists shipped in `data/`.

Lines are 1-based. A text's lines are its LF-separated pieces, so "a\\nb\\n"
and "a\\nb" both have two lines; edits keep whether the text ended with a line
break. Inserted text is taken as whole lines too: "x" and "x\\n" are one line.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Literal

from prismtex.workspace.text import strip_comment

__all__ = [
    "Ambiguous",
    "Applied",
    "Closest",
    "DamageKind",
    "EscapeDamage",
    "NotFound",
    "OutOfRange",
    "View",
    "closest_match",
    "control_char",
    "escape_damage",
    "insert_after_line",
    "known_commands",
    "numbered_around",
    "render_view",
    "replace_lines",
    "str_replace",
    "view_id",
]

DamageKind = Literal["control_char", "tab_letters", "lf_letters", "literal_backslash_n"]


@dataclass(frozen=True)
class View:
    """Numbered lines start..end of a file of total lines, under a header.

    body is "path · lines a-b of n · view <view_id>", then one "  NN| text"
    line per line shown. An empty file gives start = end = 0 and the header
    alone.
    """

    path: str
    start: int
    end: int
    total: int
    view_id: str
    body: str


@dataclass(frozen=True)
class Applied:
    """An edit that applied.

    Lines start_line..end_line (inclusive) of the new text hold the text that
    was put in. When that text is empty, end_line == start_line - 1 and
    start_line is where it would have started. note names a tolerant match,
    such as "matched ignoring indentation at lines 41-43", where the line
    numbers are those of the matched lines in the old text.
    """

    text: str
    start_line: int
    end_line: int
    note: str | None = None


@dataclass(frozen=True)
class Closest:
    """The window of lines most similar to a needle, quoted with line numbers."""

    start: int
    end: int
    similarity: float
    numbered: str


@dataclass(frozen=True)
class NotFound:
    """No rung matched. closest is None when there was nothing to compare."""

    closest: Closest | None


@dataclass(frozen=True)
class Ambiguous:
    """The first rung with a match matched more than once; one start line per match."""

    lines: tuple[int, ...]


@dataclass(frozen=True)
class OutOfRange:
    """A line number outside 0..total."""

    total: int


@dataclass(frozen=True)
class EscapeDamage:
    """A tool argument whose backslashes JSON consumed.

    line is the 1-based line of the value holding the damaged character.
    fragment names it for the message, such as "TAB+'extbf'", and command is
    the LaTeX command that was lost, such as "\\textbf", when one is known.
    """

    line: int
    kind: DamageKind
    fragment: str
    command: str | None


# ---- lines -------------------------------------------------------------------


def _split(text: str) -> list[str]:
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _join(lines: Sequence[str], like: str) -> str:
    """Join lines, ending with a line break unless `like` is non-empty and lacks one.

    An empty last line always gets its line break; without it the line is lost.
    """
    if not lines:
        return ""
    body = "\n".join(lines)
    final = like == "" or like.endswith("\n") or lines[-1] == ""
    return body + "\n" if final else body


def _numbered(lines: Sequence[str], first: int) -> str:
    width = len(str(first + len(lines) - 1))
    return "\n".join(
        "  " + str(first + i).rjust(width) + "| " + line for i, line in enumerate(lines)
    )


def _span_words(start: int, end: int) -> str:
    return f"line {start}" if start == end else f"lines {start}-{end}"


def numbered_around(text: str, line: int, radius: int) -> str:
    """Lines line-radius..line+radius of text, numbered, with " > " marking `line`.

    Other lines start with three spaces, so "   41| a" and " > 42| b" align.
    Empty when `line` is outside the text.
    """
    lines = _split(text)
    if not 1 <= line <= len(lines):
        return ""
    first, last = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(last))
    return "\n".join(
        (" > " if n == line else "   ") + str(n).rjust(width) + "| " + lines[n - 1]
        for n in range(first, last + 1)
    )


# ---- views -------------------------------------------------------------------


def view_id(text: str) -> str:
    """The first 6 hex digits of the SHA-256 of the text (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:6]


def render_view(
    path: str,
    text: str,
    start: int | None = None,
    end: int | None = None,
    *,
    max_lines: int = 250,
) -> View:
    """Numbered lines of text, at most max_lines of them.

    start defaults to 1 and end to the last line; both are clamped to the
    file, and at least one line is shown when the file has any.
    """
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    lines = _split(text)
    total = len(lines)
    vid = view_id(text)
    if total == 0:
        first = last = 0
    else:
        first = min(max(1, 1 if start is None else start), total)
        last = total if end is None else min(end, total)
        last = min(max(last, first), first + max_lines - 1)
    header = f"{path} · lines {first}-{last} of {total} · view {vid}"
    body = header + "\n" + _numbered(lines[first - 1 : last], first) if total else header
    return View(path=path, start=first, end=last, total=total, view_id=vid, body=body)


# ---- edits -------------------------------------------------------------------

_BLANKS = re.compile(r"[ \t]+")
_INDENT = re.compile(r"[ \t]*")


def _trailing_ignored(line: str) -> str:
    return line.rstrip(" \t")


def _collapsed(line: str) -> str:
    return _BLANKS.sub(" ", line).strip(" ")


# The tolerant rungs, after the exact one: how a line is normalized, and what
# the note says was ignored.
_RUNGS: tuple[tuple[Callable[[str], str], str], ...] = (
    (_trailing_ignored, "trailing whitespace"),
    (_collapsed, "indentation"),
)


def _offsets(text: str, old: str) -> list[int]:
    found: list[int] = []
    at = text.find(old)
    while at != -1:
        found.append(at)
        at = text.find(old, at + 1)
    return found


def _line_numbers(text: str, offsets: Sequence[int]) -> tuple[int, ...]:
    numbers: list[int] = []
    line, pos = 1, 0
    for offset in offsets:
        line += text.count("\n", pos, offset)
        pos = offset
        numbers.append(line)
    return tuple(numbers)


def _window_starts(
    lines: Sequence[str], needle: Sequence[str], norm: Callable[[str], str]
) -> list[int]:
    k = len(needle)
    if k == 0 or k > len(lines):
        return []
    want = [norm(line) for line in needle]
    have = [norm(line) for line in lines]
    return [i for i in range(len(lines) - k + 1) if have[i] == want[0] and have[i : i + k] == want]


def str_replace(text: str, old: str, new: str) -> Applied | NotFound | Ambiguous:
    """Replace the one match of old with new, trying each rung of the ladder.

    Rungs: exact (a substring match); then, line by line over whole lines,
    trailing spaces and tabs ignored; then runs of spaces and tabs collapsed
    with leading indentation ignored. The first rung with any match decides:
    one match applies, more than one is Ambiguous. A tolerant match replaces
    the matched lines, and the first new line takes the indentation of the
    first matched line; that is the only change ever made to new. Backslashes
    are never normalized. An empty old is NotFound with no closest match.
    """
    if not old:
        return NotFound(closest=None)
    offsets = _offsets(text, old)
    if len(offsets) == 1:
        at = offsets[0]
        result = text[:at] + new + text[at + len(old) :]
        first = text.count("\n", 0, at) + 1
        last = first + result.count("\n", at, at + len(new) - 1) if new else first - 1
        return Applied(text=result, start_line=first, end_line=last)
    if offsets:
        return Ambiguous(lines=_line_numbers(text, offsets))

    lines = _split(text)
    needle = _split(old)
    for norm, ignored in _RUNGS:
        starts = _window_starts(lines, needle, norm)
        if len(starts) > 1:
            return Ambiguous(lines=tuple(i + 1 for i in starts))
        if starts:
            i, k = starts[0], len(needle)
            replacement = _split(new)
            if replacement and replacement[0].strip(" \t"):
                indent = _INDENT.match(lines[i])
                replacement[0] = (indent.group(0) if indent else "") + replacement[0].lstrip(" \t")
            result = _join([*lines[:i], *replacement, *lines[i + k :]], text)
            return Applied(
                text=result,
                start_line=i + 1,
                end_line=i + len(replacement),
                note=f"matched ignoring {ignored} at {_span_words(i + 1, i + k)}",
            )
    return NotFound(closest=closest_match(text, old) if lines else None)


def insert_after_line(text: str, line: int, new: str) -> Applied | OutOfRange:
    """Insert the lines of new after line `line`; 0 is the top of the file."""
    lines = _split(text)
    if not 0 <= line <= len(lines):
        return OutOfRange(total=len(lines))
    inserted = _split(new)
    result = _join([*lines[:line], *inserted, *lines[line:]], text)
    return Applied(text=result, start_line=line + 1, end_line=line + len(inserted))


def replace_lines(text: str, start: int, end: int, new: str) -> Applied:
    """Replace lines start..end (inclusive) with the lines of new.

    start == end + 1 replaces nothing and inserts before line start. A range
    outside the text raises ValueError; callers pass spans they computed.
    """
    lines = _split(text)
    if not 1 <= start <= end + 1 <= len(lines) + 1:
        raise ValueError(f"lines {start}-{end} are outside a text of {len(lines)} lines")
    replacement = _split(new)
    result = _join([*lines[: start - 1], *replacement, *lines[end:]], text)
    return Applied(text=result, start_line=start, end_line=start + len(replacement) - 1)


# ---- closest match -----------------------------------------------------------

# An exact comparison costs about len(window) * len(needle) steps. The search
# stops comparing windows once it has spent this many and keeps the best one so
# far. A near miss is found exactly well within it; only a needle that
# resembles nothing runs out. The first comparison always runs, so a needle of
# a few hundred lines still costs one comparison of that size.
_COMPARE_BUDGET = 4_000_000


def _upper_bounds(lines: Sequence[str], k: int, target: str) -> list[float]:
    """For each window of k lines, an upper bound on its SequenceMatcher ratio.

    This is SequenceMatcher.quick_ratio (the shared multiset of characters),
    computed by sliding the window instead of recounting it.
    """
    need: dict[str, int] = {}
    for ch in target:
        need[ch] = need.get(ch, 0) + 1
    have: dict[str, int] = {}
    shared = 0

    def add(chunk: str) -> None:
        nonlocal shared
        for ch in chunk:
            count = have.get(ch, 0)
            if count < need.get(ch, 0):
                shared += 1
            have[ch] = count + 1

    def remove(chunk: str) -> None:
        nonlocal shared
        for ch in chunk:
            count = have[ch] - 1
            have[ch] = count
            if count < need.get(ch, 0):
                shared -= 1

    add("\n".join(lines[:k]))
    size = sum(len(line) for line in lines[:k]) + k - 1
    bounds: list[float] = []
    for i in range(len(lines) - k + 1):
        if i:
            remove(lines[i - 1])
            add(lines[i + k - 1])
            size += len(lines[i + k - 1]) - len(lines[i - 1])
        total = size + len(target)
        bounds.append(2.0 * shared / total if total else 1.0)
    return bounds


def closest_match(text: str, needle: str) -> Closest:
    """The window of the needle's line count most similar to it.

    Similarity is difflib.SequenceMatcher(autojunk=False).ratio() over the
    joined lines; the earliest window wins a tie. Windows are compared in
    order of an upper bound on their ratio, so the search stops once no
    remaining window can do better (or the compare budget is spent).
    """
    lines = _split(text)
    if not lines:
        return Closest(start=1, end=0, similarity=0.0, numbered="")
    target = "\n".join(_split(needle))
    k = min(max(1, len(_split(needle))), len(lines))
    bounds = _upper_bounds(lines, k, target)
    order = sorted(range(len(bounds)), key=lambda i: (-bounds[i], i))

    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(target)
    best, best_i = -1.0, 0
    spent = 0
    for i in order:
        # Later windows in this order have a lower bound, or the same bound
        # and a later start, so none of them can beat or tie-break the best.
        if bounds[i] < best or (bounds[i] == best and i > best_i):
            break
        if best >= 0 and spent > _COMPARE_BUDGET:
            break
        window = "\n".join(lines[i : i + k])
        matcher.set_seq1(window)
        ratio = matcher.ratio()
        spent += max(1, len(window)) * max(1, len(target))
        if ratio > best or (ratio == best and i < best_i):
            best, best_i = ratio, i
    shown = lines[best_i : best_i + k]
    return Closest(
        start=best_i + 1,
        end=best_i + k,
        similarity=best,
        numbered=_numbered(shown, best_i + 1),
    )


# ---- escape guard ------------------------------------------------------------

_C0 = re.compile(r"[\x00-\x08\x0b-\x1f]")
# fmt: off
_C0_NAMES = (
    "NUL", "SOH", "STX", "ETX", "EOT", "ENQ", "ACK", "BEL", "BS", "TAB", "LF", "VT",
    "FF", "CR", "SO", "SI", "DLE", "DC1", "DC2", "DC3", "DC4", "NAK", "SYN", "ETB",
    "CAN", "EM", "SUB", "ESC", "FS", "GS", "RS", "US",
)
# fmt: on
# The JSON escape that produces each character, so "\x08" + "egin" was "\begin".
_JSON_LETTER = {"\b": "b", "\f": "f", "\r": "r", "\t": "t", "\n": "n"}
_LETTER_RUN = re.compile(r"[A-Za-z]*")
_TAB_OR_LF_LETTERS = re.compile(r"[\t\n]([A-Za-z]+)")
_BACKSLASHES_N = re.compile(r"\\+n(?![A-Za-z])")
_DEFINITION = re.compile(
    r"\\(?:newcommand|renewcommand|DeclareMathOperator)\*?\s*"
    r"(?:\{\s*\\([A-Za-z]+)\s*\}|\\([A-Za-z]+))"
    r"|\\def\s*\\([A-Za-z]+)"
)


@cache
def _data_lines(name: str) -> frozenset[str]:
    raw = resources.files("prismtex.agent").joinpath("data", name).read_text(encoding="utf-8")
    return frozenset(line.strip() for line in raw.split("\n") if line.strip())


def _line_at(value: str, offset: int) -> int:
    return value.count("\n", 0, offset) + 1


def _fragment(name: str, letters: str) -> str:
    return f"{name}+'{letters}'" if letters else name


def control_char(value: str) -> int | None:
    """The offset of the first C0 control character other than TAB and LF."""
    found = _C0.search(value)
    return found.start() if found else None


def known_commands(texts: Iterable[str]) -> frozenset[str]:
    """data/commands.txt plus the commands the texts define, without backslashes.

    Definitions are \\newcommand, \\renewcommand, \\def and
    \\DeclareMathOperator (starred or not) outside comments.
    """
    defined: set[str] = set()
    for text in texts:
        for line in text.split("\n"):
            for found in _DEFINITION.finditer(strip_comment(line)):
                defined.add(next(name for name in found.groups() if name))
    return _data_lines("commands.txt") | defined


def escape_damage(
    value: str, known: frozenset[str], *, skip: frozenset[DamageKind] = frozenset()
) -> EscapeDamage | None:
    """Detect LaTeX whose backslashes a JSON decoder consumed; never repairs.

    Checked in this order, the first hit wins:
    1. a C0 character other than TAB and LF (a backspace from \\b, a form feed
       from \\f, a CR from \\r); the lost command is the backslash, the escape
       letter and the letters after it;
    2. a TAB or LF followed by a letter run W, when "t" + W or "n" + W is a
       known command and W is not in data/words.txt;
    3. a backslash that starts a control word, then "n", then a non-letter or
       the end: \\n is not a LaTeX command (unless the Project defines it), so
       it is a doubly escaped line break.

    `skip` leaves out the checks of the kinds it names; the others still run.
    """
    offset = control_char(value) if "control_char" not in skip else None
    if offset is not None:
        ch = value[offset]
        run = _LETTER_RUN.match(value, offset + 1)
        letters = run.group(0) if run else ""
        letter = _JSON_LETTER.get(ch)
        line_break = ch == "\r" and value.startswith("\n", offset + 1)
        command = None if letter is None or line_break else "\\" + letter + letters
        return EscapeDamage(
            line=_line_at(value, offset),
            kind="control_char",
            fragment=_fragment(_C0_NAMES[ord(ch)], letters),
            command=command,
        )

    words = _data_lines("words.txt")
    for found in _TAB_OR_LF_LETTERS.finditer(value):
        ch, letters = found.group(0)[0], found.group(1)
        if ("tab_letters" if ch == "\t" else "lf_letters") in skip:
            continue
        name = _JSON_LETTER[ch] + letters
        if name in known and letters not in words:
            return EscapeDamage(
                line=_line_at(value, found.start()),
                kind="tab_letters" if ch == "\t" else "lf_letters",
                fragment=_fragment("TAB" if ch == "\t" else "LF", letters),
                command="\\" + name,
            )

    if "n" not in known and "literal_backslash_n" not in skip:
        for found in _BACKSLASHES_N.finditer(value):
            # An even run of backslashes is a series of \\ line breaks and the
            # n is plain text; an odd run ends in a backslash that starts \n.
            if (found.end() - found.start() - 1) % 2:
                return EscapeDamage(
                    line=_line_at(value, found.end() - 2),
                    kind="literal_backslash_n",
                    fragment="\\n",
                    command=None,
                )
    return None
