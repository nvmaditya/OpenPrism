"""Agent tools (agent design §2.2 `tools`, §4.2, §4.5, §4.6).

`AGENTIC_TOOLS` are the 11 tools of the agentic Profile, sorted by name; their
specs are part of the prompt version. `ToolBox.run` runs one model tool call
against the Turn's Staging, never the Project folder, and always returns a
`ToolOutcome`: a model mistake becomes an error the model can act on, never an
exception. In order, a call is counted against the Turn's budget, then refused
when the tool is unknown, its input was cut off, its arguments do not parse or
fit the schema, a LaTeX argument shows escape damage, or it would write on a
question Turn. Staging's `ToolError` codes pass through.

The escape guard (§4.6) checks every `latex_fields` argument. A TAB or a line
break followed by letters is read as damage only when nothing in the call shows
that the model escapes backslashes. In a call that parsed as strict JSON and was
not recovered from text, a LaTeX argument holding a backslash before a letter or
before another backslash can only come from a doubled backslash, so the TABs
and line breaks of that call are taken as written. A literal backslash-n counts
as a doubly escaped line break only in an argument that holds no real line
break. Control characters are always refused.

The repeat guard keys failures on (tool, canonical arguments, error code). The
second identical failure gets `STOP_HINT` appended; the third is reported as
`repeated` and sets `gave_up`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from prismtex.agent.budget import BudgetGuard
from prismtex.agent.latex import (
    BibError,
    add_bib_entry,
    apply_edits,
    ensure_package,
    insertion_point,
    strip_fragment_wrappers,
    wire_bibliography,
)
from prismtex.agent.providers import ToolCall
from prismtex.agent.providers.toolcalls import parse_tool_arguments
from prismtex.agent.staging import StagedText, Staging, ToolError
from prismtex.agent.text import (
    Ambiguous,
    Applied,
    DamageKind,
    EscapeDamage,
    NotFound,
    OutOfRange,
    escape_damage,
    insert_after_line,
    numbered_around,
    render_view,
    str_replace,
)
from prismtex.agent.types import Intent
from prismtex.agent.verify import DiagnosticView, Verification
from prismtex.errors import RootFileMissing
from prismtex.outline import Heading, Outline

__all__ = [
    "AGENTIC_TOOLS",
    "CHECK_LIMIT",
    "MAX_RESULT_TOKENS",
    "READ_ONLY_TOOLS",
    "SEARCH_HITS",
    "STOP_HINT",
    "TRUNCATED_ARGS",
    "VIEW_LINES",
    "ToolBox",
    "ToolOutcome",
    "ToolSpec",
    "diagnostic_block",
]

VIEW_LINES: Final = 250
SEARCH_HITS: Final = 50
CHECK_LIMIT: Final = 3
MAX_RESULT_TOKENS: Final = 4_000
STOP_HINT: Final = (
    "STOP: this exact call already failed. Change old_str, or tell the user what blocks you."
)
TRUNCATED_ARGS: Final = "Your tool input was cut off at the output limit. Make a smaller edit."
NOTHING_CHANGED: Final = "Nothing was changed."

_CUT_NOTE: Final = (
    "[The result was cut here: it was longer than 4,000 tokens. "
    "View a smaller range or narrow the search.]"
)
_LINE_CHARS: Final = 200  # a search hit shows at most this much of its line
_SNIPPET_LINES: Final = 12  # an edit result shows at most this many of the new lines
_MAX_PATTERN: Final = 500
_SHOWN_DIAGNOSTICS: Final = 3


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool as a model sees it.

    `input_schema` is a JSON Schema object with `additionalProperties: false`, every
    field required and optional ones nullable. `latex_fields` names the arguments that
    carry LaTeX, which the escape guard checks (agent design §4.6).
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    read_only: bool
    latex_fields: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """The result of one tool call.

    `for_model` goes back to the model as the tool result: numbered lines, a closest
    match or an actionable error, at most 4,000 estimated tokens. `for_user` is one
    line for the tool card. `changed` lists the staged paths this call changed.
    """

    ok: bool
    for_model: str
    for_user: str
    error_code: str | None
    changed: tuple[str, ...]


# -- the tool table (§4.2) -----------------------------------------------------------


def _schema(properties: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: dict(prop) for name, prop in properties.items()},
        "required": list(properties),
        "additionalProperties": False,
    }


def _field(kind: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "description": description, **extra}


_PATH = _field("string", "A path relative to the project folder.")

AGENTIC_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="add_bib_entry",
        description=(
            "Add one BibTeX entry to the project's bibliography. PrismTeX picks the .bib file "
            "the document reads, or creates one and connects it to the document, and returns "
            "the key to cite. An entry whose key exists with different fields is refused. Add "
            "only entries the author gave you or that you know to be real."
        ),
        input_schema=_schema(
            {"bibtex": _field("string", "One complete BibTeX entry: @type{key, field = {...}}.")}
        ),
        read_only=False,
        latex_fields=frozenset({"bibtex"}),
    ),
    ToolSpec(
        name="check",
        description=(
            "Compile the project with your changes and list the problems they add compared "
            "with the project before this turn, with numbered lines. At most 3 calls per turn."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        read_only=False,
    ),
    ToolSpec(
        name="create",
        description=(
            "Create a new source file with the given text. A .tex file is part of the document "
            "only once another file includes it with \\input or \\include."
        ),
        input_schema=_schema(
            {
                "path": _field("string", "The new file's path, relative to the project folder."),
                "text": _field("string", "The complete text of the new file."),
            }
        ),
        read_only=False,
        latex_fields=frozenset({"text"}),
    ),
    ToolSpec(
        name="delete_file",
        description="Delete a source file. The deletion becomes part of your proposal.",
        input_schema=_schema({"path": _PATH}),
        read_only=False,
    ),
    ToolSpec(
        name="ensure_package",
        description=(
            "Load a LaTeX package in the preamble of the root file. Does nothing when the "
            "package is loaded already."
        ),
        input_schema=_schema(
            {
                "name": _field("string", "The package name."),
                "options": _field(["string", "null"], "Package options, comma-separated, or null."),
            }
        ),
        read_only=False,
    ),
    ToolSpec(
        name="insert",
        description=(
            "Insert lines after a line of a file, using the line numbers of your latest view "
            "of that file. after_line 0 inserts at the top."
        ),
        input_schema=_schema(
            {
                "path": _PATH,
                "after_line": _field(
                    "integer", "The line to insert after, from the view; 0 is the top."
                ),
                "text": _field("string", "The lines to insert."),
                "view_id": _field(
                    "string",
                    "The view id in the header of that view. After the file changes, view it "
                    "again.",
                ),
            }
        ),
        read_only=False,
        latex_fields=frozenset({"text"}),
    ),
    ToolSpec(
        name="insert_after_heading",
        description=(
            "Insert lines into the body of a section: at its end, or at its start after the "
            "heading and its \\label. Text never lands after the bibliography or "
            "\\end{document}."
        ),
        input_schema=_schema(
            {
                "heading": _field(
                    "string",
                    "The section's title, its \\label key, or its number in the outline.",
                ),
                "text": _field("string", "The lines to insert."),
                "where": _field(
                    ["string", "null"],
                    'Where in the section body: "start" or "end"; null means "end".',
                    enum=["start", "end", None],
                ),
            }
        ),
        read_only=False,
        latex_fields=frozenset({"text"}),
    ),
    ToolSpec(
        name="outline",
        description=(
            "Show the structure of the document as it stands with your changes: numbered "
            "headings with their file, line span and label, which files include which, the "
            "bibliography files, and the .tex files the root file never includes."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    ToolSpec(
        name="search",
        description=(
            "Search the source files, .bib files included. Returns at most 50 hits, each as "
            "path:line: text."
        ),
        input_schema=_schema(
            {
                "query": _field(
                    "string",
                    "The text to find, ignoring case; a Python regular expression when regex "
                    "is true.",
                ),
                "path": _field(
                    ["string", "null"], "Search only this file or folder; null searches all."
                ),
                "regex": _field(["boolean", "null"], "true to read query as a regular expression."),
            }
        ),
        read_only=True,
    ),
    ToolSpec(
        name="str_replace",
        description=(
            "Replace text in a file. old_str must match exactly one place; trailing spaces and "
            'indentation may differ. Copy old_str from a view, without the "NN| " prefixes.'
        ),
        input_schema=_schema(
            {
                "path": _PATH,
                "old_str": _field(
                    "string", "The exact text to replace, with enough lines to be unique."
                ),
                "new_str": _field("string", "The text to put in its place."),
            }
        ),
        read_only=False,
        latex_fields=frozenset({"old_str", "new_str"}),
    ),
    ToolSpec(
        name="view",
        description=(
            "Show numbered lines of a source file, at most 250 at a time, under a header that "
            "carries the file's view id."
        ),
        input_schema=_schema(
            {
                "path": _PATH,
                "start_line": _field(["integer", "null"], "The first line to show; null for 1."),
                "end_line": _field(["integer", "null"], "The last line to show; null for the end."),
            }
        ),
        read_only=True,
    ),
)
READ_ONLY_TOOLS: tuple[ToolSpec, ...] = tuple(spec for spec in AGENTIC_TOOLS if spec.read_only)

_SPECS: Final[Mapping[str, ToolSpec]] = {spec.name: spec for spec in AGENTIC_TOOLS}
# Tools that stage changes; their failures end with "Nothing was changed."
_WRITES: Final = frozenset(
    spec.name for spec in AGENTIC_TOOLS if not spec.read_only and spec.name != "check"
)


# -- the escape guard ----------------------------------------------------------------

# A backslash before a letter or another backslash: in strict JSON only a doubled
# backslash produces one, so the model escaped that command or line break.
_ESCAPED = re.compile(r"\\[A-Za-z\\]")
_CONTROL_WORDS: Final[Mapping[str, str]] = {
    "BS": "a backspace",
    "FF": "a form feed",
    "CR": "a carriage return",
}


def _skipped(value: str, *, escapes: bool) -> frozenset[DamageKind]:
    """The checks that do not apply to `value`; `escapes`: the call escapes backslashes."""
    skip: set[DamageKind] = set()
    if escapes:
        skip.update(("tab_letters", "lf_letters"))
    if "\n" in value:
        skip.add("literal_backslash_n")
    return frozenset(skip)


def _damage_message(field: str, damage: EscapeDamage) -> str:
    where = f"{field} line {damage.line} has {damage.fragment}"
    command = damage.command
    fix = f"Escape every backslash as \\\\ (write \\{command})." if command else ""
    if damage.kind == "control_char":
        name = damage.fragment.split("+", 1)[0]
        if command is None:
            return (
                f"{where}, a control character LaTeX source never holds. Remove it. "
                f"{NOTHING_CHANGED}"
            )
        what = _CONTROL_WORDS.get(name, "a control character")
        return (
            f"{where}: JSON turned \\{command[1]} of {command} into {what}. {fix} {NOTHING_CHANGED}"
        )
    if damage.kind == "tab_letters" and command is not None:
        letters = command[2:]
        return (
            f"{where}: JSON turned \\t of {command} into a TAB. {fix} If you meant a TAB before "
            f"'{letters}', indent with spaces instead. {NOTHING_CHANGED}"
        )
    if damage.kind == "lf_letters" and command is not None:
        letters = command[2:]
        return (
            f"{where}: JSON turned \\n of {command} into a line break. {fix} If you meant a line "
            f"break before '{letters}', start that line with a space, which TeX ignores. "
            f"{NOTHING_CHANGED}"
        )
    return (
        f"{field} line {damage.line} has \\n, which is not a LaTeX command: a line break was "
        "escaped twice. In JSON a line break is \\n, not \\\\n. If you meant the two characters "
        "\\n, as in a code listing, include the neighbouring lines so the text spans more than "
        f"one line. {NOTHING_CHANGED}"
    )


# -- the ToolBox -----------------------------------------------------------------------


class _Refused(Exception):
    """A tool call that was refused; nothing was changed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _Done:
    """What a tool handler did: the text for the model and the user, and the paths it changed."""

    for_model: str
    for_user: str
    changed: tuple[str, ...] = ()


Handler = Callable[[Mapping[str, Any]], Awaitable[_Done]]


class ToolBox:
    """Runs the agentic tools of one Turn against its Staging.

    `outline` returns the Outline of the staged text; `check` compiles the staged tree
    against the Baseline. `known_commands` feeds the escape guard. On a question Turn
    every tool but outline, search and view is refused with `read_only_turn`.
    """

    def __init__(
        self,
        staging: Staging,
        outline: Callable[[], Outline],
        check: Callable[[], Awaitable[Verification]],
        budget: BudgetGuard,
        known_commands: frozenset[str],
        intent: Intent,
    ) -> None:
        self._staging = staging
        self._outline = outline
        self._check = check
        self._budget = budget
        self._known = known_commands
        self._intent: Intent = intent
        self._checks = 0
        self._failures: dict[tuple[str, str, str], int] = {}
        self._gave_up = False
        self._handlers: Mapping[str, Handler] = {
            "add_bib_entry": self._add_bib_entry,
            "check": self._run_check,
            "create": self._create,
            "delete_file": self._delete_file,
            "ensure_package": self._ensure_package,
            "insert": self._insert,
            "insert_after_heading": self._insert_after_heading,
            "outline": self._show_outline,
            "search": self._search,
            "str_replace": self._str_replace,
            "view": self._view,
        }

    def specs(self, memory: Literal["transcript", "records"]) -> tuple[ToolSpec, ...]:
        """The tools to send: all of them, except `READ_ONLY_TOOLS` for records memory on a
        question Turn (transcript memory keeps the tool array constant in a Thread)."""
        if memory == "records" and self._intent == "question":
            return READ_ONLY_TOOLS
        return AGENTIC_TOOLS

    @property
    def gave_up(self) -> bool:
        """True once the repeat guard refused a call that had failed twice the same way."""
        return self._gave_up

    async def run(self, call: ToolCall) -> ToolOutcome:
        """Run one call. A model mistake never raises; the Turn's limits do.

        Raises `BudgetExhausted` for the tool-call limit or wall time, and
        `Cancelled` when the Turn is cancelled during `check`.
        """
        self._budget.tool_call()
        outcome, canonical = await self._outcome(call)
        return self._guard(call.name, canonical, self._fitted(outcome))

    # -- the order of refusals ---------------------------------------------------------

    async def _outcome(self, call: ToolCall) -> tuple[ToolOutcome, str]:
        spec = _SPECS.get(call.name)
        if spec is None:
            names = ", ".join(_SPECS)
            message = f"There is no tool named '{call.name}'. The tools are: {names}."
            return _failed(call.name, "unknown_tool", message), call.raw_args
        if call.parse_error == "truncated":
            outcome = ToolOutcome(
                False, TRUNCATED_ARGS, f"{call.name}: input cut off", "truncated_args", ()
            )
            return outcome, call.raw_args
        raw = call.raw_args if call.raw_args.strip() else "{}"
        parsed = parse_tool_arguments(raw, spec.input_schema)
        if parsed.args is None:
            detail = (parsed.error or "invalid_args").removeprefix("invalid_args").lstrip(": ")
            if detail.startswith("at "):  # a JSON syntax error and its position
                message = f"The arguments of {spec.name} are not valid JSON {detail}"
            else:
                message = f"The arguments of {spec.name} are not valid: {detail}."
            return _failed(spec.name, "invalid_args", message), call.raw_args
        args = _normalized(parsed.args, spec)
        canonical = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        latex = [
            (name, value)
            for name in spec.input_schema["properties"]
            if name in spec.latex_fields and isinstance(value := args.get(name), str)
        ]
        escapes = (
            not parsed.lenient
            and not call.recovered
            and any(_ESCAPED.search(value) for _, value in latex)
        )
        for name, value in latex:
            damage = escape_damage(value, self._known, skip=_skipped(value, escapes=escapes))
            if damage is not None:
                message = _damage_message(name, damage)
                return _failed(spec.name, "escape_damage", message), canonical
        if self._intent == "question" and not spec.read_only:
            message = (
                f"This turn is a question: answer it and change nothing. {spec.name} was refused."
            )
            return _failed(spec.name, "read_only_turn", message), canonical
        mark = self._staging.mark()
        try:
            done = await self._handlers[spec.name](args)
        except ToolError as error:
            message = error.message
            if error.code == "not_found" and error.path is not None:
                message = self._did_you_mean(message, error.path)
            return _failed(spec.name, error.code, message), canonical
        except _Refused as refused:
            return _failed(spec.name, refused.code, refused.message), canonical
        changed = done.changed if self._staging.changed_since(mark) else ()
        return ToolOutcome(True, done.for_model, done.for_user, None, changed), canonical

    def _guard(self, name: str, canonical: str, outcome: ToolOutcome) -> ToolOutcome:
        if outcome.ok:
            return outcome
        digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
        key = (name, digest, outcome.error_code or "")
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count == 2:
            return ToolOutcome(
                False,
                f"{outcome.for_model}\n{STOP_HINT}",
                outcome.for_user,
                outcome.error_code,
                (),
            )
        if count >= 3:
            self._gave_up = True
            message = (
                f"Refused: this exact {name} call has now failed {count} times in the same way. "
                "Stop calling tools and tell the user what blocks you."
            )
            return ToolOutcome(
                False,
                _ending(name, message),
                f"{name}: stopped after repeated failures",
                "repeated",
                (),
            )
        return outcome

    def _did_you_mean(self, message: str, path: str) -> str:
        close = difflib.get_close_matches(path, self._staging.paths(), n=3)
        return f"{message} Did you mean: {', '.join(close)}?" if close else message

    def _fitted(self, outcome: ToolOutcome) -> ToolOutcome:
        """The outcome with `for_model` cut to 4,000 estimated tokens, closing sentence kept."""
        text = outcome.for_model
        per_token = self._budget.spec.chars_per_token
        if math.ceil(len(text) / per_token) <= MAX_RESULT_TOKENS:
            return outcome
        tail = f" {NOTHING_CHANGED}" if text.endswith(NOTHING_CHANGED) else ""
        room = int(MAX_RESULT_TOKENS * per_token) - len(_CUT_NOTE) - len(tail) - 1
        cut = text.rfind("\n", 0, room)
        cut_text = text[: cut if cut > 0 else room] + "\n" + _CUT_NOTE + tail
        return ToolOutcome(
            outcome.ok, cut_text, outcome.for_user, outcome.error_code, outcome.changed
        )

    def _read(self, path: str) -> StagedText:
        return self._staging.read(path)

    def _root(self, purpose: str) -> str:
        root = self._staging.base.root_file
        if root is None:
            raise _Refused("not_found", f"The project has no root file, so {purpose}.")
        return root

    def _current_outline(self) -> Outline:
        try:
            return self._outline()
        except RootFileMissing as error:
            raise _Refused(
                "not_found", "The project has no root file, so it has no outline."
            ) from error

    # -- read tools ----------------------------------------------------------------------

    async def _view(self, args: Mapping[str, Any]) -> _Done:
        staged = self._read(args["path"])
        start, end = args["start_line"], args["end_line"]
        total = len(_lines(staged.text))
        if start is not None and total and start > total:
            raise _Refused(
                "out_of_range",
                f"start_line {start} is past the end of {staged.path}, which has {total} lines.",
            )
        view = render_view(staged.path, staged.text, start, end, max_lines=VIEW_LINES)
        body = view.body
        if view.end < view.total and (end is None or end > view.end):
            body += (
                f"\n[Lines {view.start}-{view.end} of {view.total} shown. "
                f"View from line {view.end + 1} for more.]"
            )
        return _Done(body, f"Viewed {staged.path} {_span(view.start, view.end)}")

    async def _search(self, args: Mapping[str, Any]) -> _Done:
        query: str = args["query"]
        where: str | None = args["path"]
        if not query:
            raise _Refused("invalid_args", "query is empty.")
        if args["regex"]:
            if len(query) > _MAX_PATTERN:
                raise _Refused("invalid_args", "The regular expression is too long.")
            try:
                pattern = re.compile(query)
            except re.error as error:
                raise _Refused(
                    "invalid_args", f"query is not a valid regular expression: {error}."
                ) from error

            def matches(line: str) -> bool:
                return pattern.search(line) is not None
        else:
            needle = query.casefold()

            def matches(line: str) -> bool:
                return needle in line.casefold()

        paths = self._staging.paths()
        if where is not None and where.strip():
            folder = where.strip().rstrip("/")
            paths = [p for p in paths if p == folder or p.startswith(folder + "/")]
            if not paths:
                self._read(where)  # raises the reason: denied, missing or not text
                paths = [where]
        hits: list[str] = []
        for path in paths:
            try:
                staged = self._read(path)
            except ToolError:
                continue  # an Asset: only its name is visible
            for number, line in enumerate(_lines(staged.text), start=1):
                if matches(line):
                    hits.append(f"{staged.path}:{number}: {line[:_LINE_CHARS]}")
        scope = f"in {where.strip()}" if where else "in the project"
        if not hits:
            return _Done(
                f"No matches for '{query}' {scope}.", f"Searched for '{query[:60]}': 0 hits"
            )
        shown = hits[:SEARCH_HITS]
        body = "\n".join(shown)
        if len(hits) > len(shown):
            body += (
                f"\n[{len(hits) - len(shown)} more hits not shown. Narrow the query or the path.]"
            )
        return _Done(body, f"Searched for '{query[:60]}': {len(hits)} hits")

    async def _show_outline(self, args: Mapping[str, Any]) -> _Done:
        outline = self._current_outline()
        count = len(outline.headings)
        return _Done(
            _render_outline(outline), f"Outline: {count} heading{'' if count == 1 else 's'}"
        )

    # -- edit tools ----------------------------------------------------------------------

    async def _str_replace(self, args: Mapping[str, Any]) -> _Done:
        old: str = args["old_str"]
        new: str = args["new_str"]
        staged = self._read(args["path"])
        path = staged.path
        if not old:
            raise _Refused(
                "invalid_args", "old_str is empty. To add text, use insert or insert_after_heading."
            )
        if old == new:
            raise _Refused("invalid_args", "old_str and new_str are the same.")
        result = str_replace(staged.text, old, new)
        if isinstance(result, Ambiguous):
            places = ", ".join(str(n) for n in result.lines)
            raise _Refused(
                "ambiguous",
                f"old_str matches {len(result.lines)} places (lines {places}). Include "
                "neighbouring lines to make it unique.",
            )
        if isinstance(result, NotFound):
            total = len(_lines(staged.text))
            head = f"str_replace failed: old_str was not found in {path} ({total} lines)."
            closest = result.closest
            if closest is None or not closest.numbered:
                raise _Refused("not_found", head)
            raise _Refused(
                "not_found",
                f"{head}\nClosest match (similarity {closest.similarity:.2f}) at "
                f"{_span_words(closest.start, closest.end)}:\n{closest.numbered}\n"
                'Copy old_str exactly from these lines, without the "NN| " prefixes.',
            )
        return self._stage_edit(path, result, verb="Replaced")

    async def _insert(self, args: Mapping[str, Any]) -> _Done:
        text: str = args["text"]
        after: int = args["after_line"]
        staged = self._read(args["path"])
        if staged.view_id != args["view_id"]:
            raise _Refused(
                "stale_view",
                f"{staged.path} has changed since view {args['view_id']}, or that view is not of "
                "this file. View it again and use the line numbers of the new view.",
            )
        if not text:
            raise _Refused("invalid_args", "text is empty.")
        result = insert_after_line(staged.text, after, text)
        if isinstance(result, OutOfRange):
            raise _Refused(
                "out_of_range",
                f"after_line {after} is outside {staged.path}, which has {result.total} lines; "
                f"use 0 to {result.total}.",
            )
        return self._stage_edit(staged.path, result, verb="Inserted")

    async def _insert_after_heading(self, args: Mapping[str, Any]) -> _Done:
        text: str = args["text"]
        query: str = args["heading"]
        where: Literal["start", "end"] = args["where"] or "end"
        if not text:
            raise _Refused("invalid_args", "text is empty.")
        outline = self._current_outline()
        heading = _one_heading(outline, query)
        path, after = insertion_point(outline, heading, where=where)
        staged = self._read(path)
        result = insert_after_line(staged.text, after, text)
        if isinstance(result, OutOfRange):  # the Outline was built from this text
            raise _Refused("out_of_range", f"{path} changed under the outline; try again.")
        return self._stage_edit(staged.path, result, verb="Inserted")

    async def _create(self, args: Mapping[str, Any]) -> _Done:
        text: str = args["text"]
        notes: list[str] = []
        if str(args["path"]).lower().endswith(".tex"):
            stripped, removed = strip_fragment_wrappers(text)
            if removed and not any(r.startswith("\\documentclass") for r in removed):
                text = stripped
                notes.append(
                    f"Removed {' and '.join(removed)}: an included file is part of the "
                    "document, not a document of its own."
                )
        change = self._staging.create(args["path"], text, by="model")
        path = change.path
        if path.lower().endswith(".tex"):
            try:
                unincluded = path in self._outline().unincluded
            except RootFileMissing:
                unincluded = False
            if unincluded:
                notes.append(
                    f"The root file does not include {path} yet: add \\input{{{path[:-4]}}} "
                    "where it belongs, or it will not appear in the document."
                )
        lines = len(_lines(text))
        body = " ".join([f"Created {path} with {lines} line{'' if lines == 1 else 's'}.", *notes])
        return _Done(body, f"Created {path}", (path,))

    async def _delete_file(self, args: Mapping[str, Any]) -> _Done:
        change = self._staging.delete(args["path"], by="model")
        return _Done(
            f"Deleted {change.path}. The deletion is part of your proposal.",
            f"Deleted {change.path}",
            (change.path,),
        )

    async def _ensure_package(self, args: Mapping[str, Any]) -> _Done:
        name = str(args["name"]).strip()
        options = args["options"] or ""
        root = self._root("there is no preamble to load a package in")
        staged = self._read(root)
        try:
            edit = ensure_package(staged.text, name, options, root_file=root)
        except ValueError as error:
            raise _Refused("invalid_args", str(error)) from error
        if edit is None:
            return _Done(f"{name} is loaded already in {root}.", f"{name} is loaded already")
        if not edit.lines:
            return _Done(f"{name} is loaded already with other options: {edit.note}.", edit.note)
        self._staging.write(root, apply_edits(staged.text, [edit]), by="host")
        line = edit.lines[0]
        return _Done(
            f"Added {line} to {root} after line {edit.after_line}.",
            f"Added {line} to {root}",
            (staged.path,),
        )

    async def _add_bib_entry(self, args: Mapping[str, Any]) -> _Done:
        root = self._root("a bibliography entry cannot be connected to the document")
        outline = self._current_outline()
        texts: dict[str, str] = {}
        for path in self._staging.paths():
            if path.lower().endswith(".bib"):
                try:
                    texts[path] = self._read(path).text
                except ToolError:
                    continue
        try:
            result = add_bib_entry(texts, outline, args["bibtex"])
        except BibError as error:
            raise _Refused(
                "invalid_args" if error.reason == "parse" else "exists", error.message
            ) from error
        if result.reused:
            return _Done(
                f"That entry is in {result.path} already. Cite it as \\cite{{{result.key}}}.",
                f"Entry {result.key} is in {result.path} already",
            )
        changed: list[str] = []
        for edit in result.edits:
            if result.created:
                change = self._staging.create(edit.path, apply_edits("", [edit]), by="host")
            else:
                change = self._staging.write(
                    edit.path, apply_edits(texts[edit.path], [edit]), by="host"
                )
            changed.append(change.path)
        root_text = self._read(root).text
        try:
            wiring = wire_bibliography(root_text, outline, result.path, root_file=root)
            unwired = ""
        except ValueError:  # a .bib name TeX cannot read from \bibliography
            wiring = ()
            unwired = f" {result.path} could not be connected to the document automatically."
        if wiring:
            self._staging.write(root, apply_edits(root_text, list(wiring)), by="host")
            changed.append(root)
        wired = f" and connected it to the document in {root}" if wiring else ""
        return _Done(
            f"Added the entry {result.key} to {result.path}{wired}.{unwired} Cite it as "
            f"\\cite{{{result.key}}}. The author checks every entry you add.",
            f"Added bibliography entry {result.key} to {result.path}",
            tuple(dict.fromkeys(changed)),
        )

    async def _run_check(self, args: Mapping[str, Any]) -> _Done:
        self._checks += 1
        if self._checks > CHECK_LIMIT:
            raise _Refused(
                "check_limit",
                f"check ran {CHECK_LIMIT} times this turn, the most it may. PrismTeX compiles "
                "your changes when you finish.",
            )
        verification = await self._check()
        return _Done(*_render_check(verification, self._staging))

    def _stage_edit(self, path: str, result: Applied, *, verb: str) -> _Done:
        if result.text == self._read(path).text:
            return _Done(f"{path} already reads that way; nothing changed.", f"{path} unchanged")
        self._staging.write(path, result.text, by="model")
        staged = self._read(path)
        lines = _lines(staged.text)
        start, end = result.start_line, result.end_line
        note = f" ({result.note})" if result.note else ""
        if end < start:
            body = f"Removed text at line {start} of {path}{note}."
            user = f"Edited {path} at L{start}"
        else:
            body = (
                f"{verb} text in {path}{note}. {_span_words(start, end).capitalize()} now "
                f"read:\n{_snippet(lines, start, end)}"
            )
            user = f"{verb} text in {path} {_span(start, end)}"
        body += f"\n{path} now has {len(lines)} lines · view {staged.view_id}"
        return _Done(body, user, (path,))


# -- helpers ---------------------------------------------------------------------------


def _failed(name: str, code: str, message: str) -> ToolOutcome:
    text = _ending(name, message)
    first = message.split("\n", 1)[0]
    user = f"{name}: {first}"
    if len(user) > 160:
        user = user[:159].rstrip() + "…"
    return ToolOutcome(False, text, user, code, ())


def _ending(name: str, message: str) -> str:
    if name in _WRITES and not message.endswith(NOTHING_CHANGED):
        return f"{message} {NOTHING_CHANGED}"
    return message


def _normalized(args: Mapping[str, Any], spec: ToolSpec) -> dict[str, Any]:
    """The arguments with CRLF line breaks in LaTeX fields as LF, as Staging stores them."""
    out = dict(args)
    for name in spec.latex_fields:
        value = out.get(name)
        if isinstance(value, str):
            out[name] = value.replace("\r\n", "\n")
    return out


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _span(start: int, end: int) -> str:
    return f"L{start}" if start == end else f"L{start}-L{end}"


def _span_words(start: int, end: int) -> str:
    return f"line {start}" if start == end else f"lines {start}-{end}"


def _snippet(lines: list[str], start: int, end: int) -> str:
    last = min(end, start + _SNIPPET_LINES - 1)
    width = len(str(last))
    shown = [f"  {str(n).rjust(width)}| {lines[n - 1]}" for n in range(start, last + 1)]
    if last < end:
        shown.append(f"  [{end - last} more lines]")
    return "\n".join(shown)


def _heading_ref(heading: Heading) -> str:
    return f"{heading.index} {heading.title} ({heading.path}:{heading.line})"


def _one_heading(outline: Outline, query: str) -> Heading:
    found = outline.find_heading(query)
    if len(found) > 1:
        wanted = " ".join(query.split()).casefold()
        exact = [h for h in found if h.title.casefold() == wanted]
        found = tuple(exact) if len(exact) == 1 else found
    if len(found) == 1:
        return found[0]
    if found:
        listed = "; ".join(_heading_ref(h) for h in found[:8])
        raise _Refused(
            "ambiguous",
            f"Several headings match '{query}': {listed}. Give its number or its \\label key.",
        )
    listed = "; ".join(_heading_ref(h) for h in outline.headings[:30]) or "none"
    raise _Refused("not_found", f"No heading matches '{query}'. The headings are: {listed}.")


_LEVEL_COMMANDS: Final[Mapping[int, str]] = {
    -1: "part",
    0: "chapter",
    1: "section",
    2: "subsection",
    3: "subsubsection",
    4: "paragraph",
    5: "subparagraph",
}


def _render_outline(outline: Outline) -> str:
    head = f"Root file: {outline.root_file}"
    if outline.document_class:
        head += f" (document class {outline.document_class})"
    lines = [head]
    if outline.headings:
        base = min(h.level for h in outline.headings)
        lines.append("Headings (number, file:lines, heading):")
        for h in outline.headings:
            command = _LEVEL_COMMANDS.get(h.level, "section")
            label = f" \\label{{{h.label}}}" if h.label else ""
            indent = "  " * (h.level - base)
            lines.append(
                f"{indent}{h.index}. {h.path}:L{h.line}-L{h.end_line} \\{command}{{{h.title}}}"
                f"{label}"
            )
    else:
        lines.append("No headings.")
    for source, targets in outline.inputs.items():
        if targets:
            lines.append(f"{source} includes {', '.join(targets)}")
    if outline.bib_files:
        keys = len(outline.bib_keys)
        lines.append(
            f"Bibliography: {', '.join(outline.bib_files)} ({keys} key{'' if keys == 1 else 's'})"
        )
    if outline.packages:
        lines.append(f"Packages: {', '.join(sorted(outline.packages))}")
    if outline.unincluded:
        lines.append(f"Not included by the root file: {', '.join(outline.unincluded)}")
    return "\n".join(lines)


_UNVERIFIED: Final[Mapping[str, str]] = {
    "cancelled": "the turn was stopped",
    "tex_unavailable": "TeX was not found",
    "timeout": "the compile took too long",
    "masked": "the errors the project had before this turn hide any new ones",
    "no_root_file": "the project has no root file",
}


def _render_check(verification: Verification, staging: Staging) -> tuple[str, str]:
    if verification.verdict == "unverified":
        why = _UNVERIFIED.get(verification.reason or "", "it could not be compiled")
        return f"Not compile-checked: {why}.", "Not compile-checked"
    if verification.verdict == "no_change":
        return "Nothing is staged yet, so there is nothing to check.", "Nothing to check"
    lines: list[str] = []
    if not verification.new:
        lines.append("Compiles with no new problems.")
        if verification.fixed:
            fixed = len(verification.fixed)
            lines.append(f"Your changes fix {fixed} problem{'' if fixed == 1 else 's'}.")
        user = "Compiles with no new problems"
    else:
        count = len(verification.new)
        lines.append(f"Your changes add {count} problem{'' if count == 1 else 's'}:")
        for diagnostic in verification.new[:_SHOWN_DIAGNOSTICS]:
            lines.extend(diagnostic_block(diagnostic, staging, radius=3))
        if count > _SHOWN_DIAGNOSTICS:
            lines.append(f"[{count - _SHOWN_DIAGNOSTICS} more not shown.]")
        user = f"{count} new problem{'' if count == 1 else 's'}"
    for fix in verification.host_fixes:
        lines.append(f"The host already {fix}.")
    return "\n".join(lines), user


def diagnostic_block(d: DiagnosticView, staging: Staging, *, radius: int) -> list[str]:
    """One Diagnostic as the model sees it: `file:line: message`, then numbered lines.

    The lines come from the staged text, `radius` on each side, the problem line
    marked " > "; the Diagnostic's excerpt stands in when the file cannot be read.
    """
    where = f"{d.file}:{d.line}" if d.file and d.line else (d.file or "(no file)")
    out = [f"{where}: {' '.join(d.message.split())}"]
    context = ""
    if d.file and d.line:
        try:
            context = numbered_around(staging.read(d.file).text, d.line, radius)
        except ToolError:
            context = ""
    if context:
        out.append(context)
    elif d.excerpt:
        out.append(f" > {d.excerpt}")
    return out
