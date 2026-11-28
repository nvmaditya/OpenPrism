"""The agentic Profile: the model reads and edits through tools (agent design §4.2).

One instance serves one Turn and keeps its conversation across Repair rounds:

1. The first user message is packed from the volatile block (§5.1): earlier
   Turns, the "since your last Turn" note, the project line, the Outline, the
   open Proposal, the Baseline Diagnostics, the editor context, the forced read
   for a question (§4.1) and the request.
2. Each Step is a `reply` Step; its assistant message joins the conversation as
   received.
3. A refusal ends the run with `stop="refused"`.
4. Tool calls run one at a time, in call order, so two edits to one file both
   apply; all results of a Step go back in one user message, with the repeat
   guard's STOP hint as a text block after them. A call a `max_tokens` stop left
   open is answered with the `truncated_args` result.
5. A Step with no tool calls ends the run (`stop="idle"`); a third identical
   failure ends it with `stop="gave_up"`.
6. For models with the `recover_text_tool_calls` quirk, tool calls written as
   JSON text are recovered (`notice(text_tool_call_recovered)`), and one that
   names a tool but cannot be read gets one re-prompt per Turn.
7. A Repair round appends one user message listing the new compile errors to
   the same conversation and loops again with the same tools.

Under transcript memory the Turn replays the Thread's segment, or opens a new
one that starts with `<earlier_turns>`; `segment` is the one it opened and
`messages` the messages it added, for the Thread.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Final, cast

from prismtex.agent.context import (
    PRIORITY,
    ContextAssembler,
    Part,
    SegmentKey,
    TokenBudget,
    open_segment,
)
from prismtex.agent.events import (
    DecisionRecord,
    NoticeEvent,
    ToolCallEvent,
    ToolErrorCode,
    ToolIORecord,
    ToolResultEvent,
)
from prismtex.agent.providers import (
    Message,
    ModelCall,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
)
from prismtex.agent.providers.catalog import RECOVER_TEXT_TOOL_CALLS
from prismtex.agent.providers.toolcalls import parse_tool_arguments, recover_text_tool_calls
from prismtex.agent.staging import Staging, ToolError
from prismtex.agent.steps import StepResult
from prismtex.agent.text import render_view
from prismtex.agent.threads import Segment
from prismtex.agent.tools import STOP_HINT, ToolOutcome, ToolSpec, diagnostic_block
from prismtex.agent.turn import MAX_REPAIR_ROUNDS, ProfileResult, RepairFeedback, TurnScope
from prismtex.agent.types import EditorContext, ProfileName
from prismtex.errors import RootFileMissing
from prismtex.outline import Outline

__all__ = [
    "EDITOR_RADIUS",
    "EXCERPT_TOKENS",
    "QUESTION_NOTE",
    "AgenticProfile",
    "repair_message",
]

QUESTION_NOTE: Final = "This is a question: answer it and change nothing."
EXCERPT_TOKENS: Final = 9_000  # the forced read is capped here (agent design §4.1)
EDITOR_RADIUS: Final = 20  # numbered lines around the selection or cursor (agent design §5.1)

# Caps of the agentic volatile block, in estimated tokens (agent design §5.3).
_PROJECT_CAP: Final = 1_000
_OUTLINE_CAP: Final = 2_500
_EDITOR_CAP: Final = 3_000
_DIAGNOSTICS_CAP: Final = 600
_LISTED_FILES: Final = 40
_ALL_LINES: Final = 10**9


class AgenticProfile:
    """The agentic loop of one Turn (agent design §4.2)."""

    name: ClassVar[ProfileName] = "agentic"

    def __init__(self) -> None:
        self._system = ""
        self._tools: tuple[ToolSpec, ...] = ()
        self._history: tuple[Message, ...] = ()
        self._messages: list[Message] = []
        self._segment: Segment | None = None
        self._started = False
        self._reprompted = False
        self._recovered = 0
        self._attempted_write = False
        self._refusal: str | None = None

    @property
    def messages(self) -> tuple[Message, ...]:
        """The messages this Turn added to the conversation, in order."""
        return tuple(self._messages)

    @property
    def segment(self) -> Segment | None:
        """The transcript segment this Turn opened, or None when it continued one."""
        return self._segment

    @property
    def refusal_category(self) -> str | None:
        """The Provider's refusal category when a run ended with `stop="refused"`."""
        return self._refusal

    async def run(self, scope: TurnScope, feedback: RepairFeedback | None) -> ProfileResult:
        if not self._started:
            self._open(scope)
        elif feedback is not None:
            text = repair_message(feedback, scope.staging)
            self._messages.append(Message("user", (TextBlock(text),)))
        return await self._loop(scope)

    # -- the first message -----------------------------------------------------------------

    def _open(self, scope: TurnScope) -> None:
        spec = scope.spec
        context = scope.context
        self._system = context.system("agentic", spec)
        self._tools = scope.tools.specs(spec.memory)
        if scope.intent == "edit":
            scope.prime_baseline()
        budget = TokenBudget.for_spec(spec, spec.max_output)
        history: tuple[Message, ...] = ()
        earlier: str | None = None
        if spec.memory == "transcript":
            history = scope.memory.transcript
            version = context.prompts.version("agentic", self._tools)
            key = SegmentKey.of(spec, "agentic", version)
            block = context.pack(_parts(scope, replayed=history), budget, fixed=0)
            plan = context.plan_segment(scope.memory, block.tokens, spec, key)
            if plan == "new":
                self._segment = open_segment(scope.memory, key)
                history, earlier = (), self._segment.summary
            scope.emit(
                DecisionRecord(
                    kind="segment",
                    choice=plan,
                    detail={"opened_by": self._segment.opened_by} if self._segment else {},
                )
            )
        parts = _parts(scope, replayed=history, earlier=earlier)
        fixed = context.estimate(
            ModelCall(self._system, history, self._tools, max_output_tokens=spec.max_output)
        )
        packed = context.pack(parts, budget, fixed=fixed)
        if packed.trimmed:
            scope.emit(
                NoticeEvent(
                    code="prompt_trimmed",
                    message="Some context was shortened or left out to fit the model: "
                    + ", ".join(packed.trimmed)
                    + ".",
                )
            )
        scope.emit(
            DecisionRecord(
                kind="context",
                choice="packed",
                detail={"parts": dict(packed.parts), "trimmed": list(packed.trimmed)},
            )
        )
        self._history = history
        self._messages.append(Message("user", (TextBlock(packed.text),)))
        self._started = True

    # -- the loop ----------------------------------------------------------------------------

    async def _loop(self, scope: TurnScope) -> ProfileResult:
        reply = ""
        while True:
            scope.cancel.raise_if_set()
            call = ModelCall(
                system=self._system,
                messages=(*self._history, *self._messages),
                tools=self._tools,
                max_output_tokens=scope.spec.max_output,
            )
            result = await scope.steps.run(call, kind="reply")
            self._messages.append(result.message)
            if result.text.strip():
                reply = result.text.strip()
            if result.stop == "refusal":
                self._refusal = result.refusal_category
                return ProfileResult(reply, "refused", self._edit_intent(scope))
            calls = _answerable(result)
            if not calls and RECOVER_TEXT_TOOL_CALLS in scope.spec.quirks:
                known = frozenset(tool.name for tool in self._tools)
                found, reason = recover_text_tool_calls(result.text, known)
                if found:
                    calls = self._take_recovered(scope, found)
                elif reason is not None and not self._reprompted:
                    self._reprompted = True
                    text = (
                        f"Your tool call was written as text and could not be read: {reason}. "
                        "Call the tool with the tool-calling format."
                    )
                    self._messages.append(Message("user", (TextBlock(text),)))
                    continue
            if not calls:
                return ProfileResult(reply, "idle", self._edit_intent(scope))
            outcomes = await self._run_tools(scope, result.message_id or "", calls)
            if any(outcome.error_code == "repeated" for outcome in outcomes):
                return ProfileResult(reply, "gave_up", self._edit_intent(scope))

    async def _run_tools(
        self, scope: TurnScope, message_id: str, calls: Sequence[ToolCall]
    ) -> list[ToolOutcome]:
        blocks: list[ToolResultBlock | TextBlock] = []
        outcomes: list[ToolOutcome] = []
        stop = False
        for call in calls:
            scope.cancel.raise_if_set()
            scope.emit(
                ToolCallEvent(
                    call_id=call.id,
                    message_id=message_id,
                    name=call.name,
                    args=_preview(call),
                    recovered=call.recovered,
                )
            )
            try:
                outcome = await scope.tools.run(call)
            except BaseException:
                scope.emit(
                    ToolResultEvent(
                        call_id=call.id,
                        ok=False,
                        summary=f"{call.name}: not run, the turn stopped",
                        error_code="cancelled",
                        changed=(),
                    )
                )
                raise
            content = outcome.for_model
            if content.endswith(STOP_HINT):
                content = content[: -len(STOP_HINT)].rstrip("\n")
                stop = True
            scope.emit(
                ToolResultEvent(
                    call_id=call.id,
                    ok=outcome.ok,
                    summary=outcome.for_user,
                    error_code=cast("ToolErrorCode | None", outcome.error_code),
                    changed=outcome.changed,
                )
            )
            scope.emit(
                ToolIORecord(
                    call_id=call.id,
                    name=call.name,
                    args=call.raw_args,
                    result=outcome.for_model,
                    ok=outcome.ok,
                    error_code=outcome.error_code,
                )
            )
            blocks.append(ToolResultBlock(call.id, content, is_error=not outcome.ok))
            outcomes.append(outcome)
            if call.name not in ("outline", "search", "view", "check"):
                self._attempted_write = True
            if outcome.changed:
                scope.prime_baseline()
        if stop:
            blocks.append(TextBlock(STOP_HINT))
        self._messages.append(Message("user", tuple(blocks)))
        return outcomes

    def _take_recovered(self, scope: TurnScope, found: Sequence[ToolCall]) -> list[ToolCall]:
        """Number the recovered calls for this Turn and put them in the assistant message."""
        calls: list[ToolCall] = []
        for call in found:
            self._recovered += 1
            calls.append(
                ToolCall(
                    id=f"rec_{self._recovered}",
                    name=call.name,
                    raw_args=call.raw_args,
                    args=call.args,
                    parse_error=call.parse_error,
                    recovered=True,
                )
            )
        # The adapter renders the calls from the blocks, so the results that follow
        # answer calls the conversation shows.
        last = self._messages[-1]
        uses = tuple(ToolUseBlock(c.id, c.name, dict(c.args or {})) for c in calls)
        self._messages[-1] = Message("assistant", (*last.blocks, *uses), None)
        count = len(calls)
        scope.emit(
            NoticeEvent(
                code="text_tool_call_recovered",
                message=f"The model wrote {count} tool call{'' if count == 1 else 's'} as text; "
                f"{'it was' if count == 1 else 'they were'} read and run.",
            )
        )
        return calls

    def _edit_intent(self, scope: TurnScope) -> bool:
        return scope.intent == "edit" or (scope.intent == "unknown" and self._attempted_write)


def repair_message(feedback: RepairFeedback, staging: Staging) -> str:
    """The user message of a Repair round (agent design §4.2 step 7).

    Each new error is `file:line: message` with ±1 numbered lines of the staged
    text, the error line marked " > ".
    """
    problems = [d for d in feedback.new if d.severity == "error"] or list(feedback.new)
    count = len(problems)
    lines = [
        f"Your changes introduced {count} new compile error{'' if count == 1 else 's'} "
        f"(repair round {feedback.round} of {MAX_REPAIR_ROUNDS}):"
    ]
    for problem in problems:
        lines.extend(diagnostic_block(problem, staging, radius=1))
    tail = f"Fix the error{'' if count == 1 else 's'} with the edit tools and change nothing else."
    if feedback.host_fixes:
        tail = f"The host already {' and '.join(feedback.host_fixes)}. {tail}"
    lines.append(tail)
    return "\n".join(lines)


# -- tool calls ------------------------------------------------------------------------------


def _answerable(result: StepResult) -> list[ToolCall]:
    """The Step's tool calls, plus any tool use the message holds without a completed call.

    A `max_tokens` stop leaves such a call open; it is answered as truncated, so
    every tool use gets a result.
    """
    calls = list(result.tool_calls)
    seen = {call.id for call in calls}
    for block in result.message.blocks:
        if isinstance(block, ToolUseBlock) and block.id not in seen:
            cut = result.stop == "max_tokens"
            calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    raw_args="" if cut else json.dumps(dict(block.input), ensure_ascii=False),
                    args=None if cut else block.input,
                    parse_error="truncated" if cut else None,
                    recovered=False,
                )
            )
    return calls


def _preview(call: ToolCall) -> Mapping[str, Any]:
    """The call's arguments for the tool card: leniently parsed, or none."""
    if call.parse_error is not None:
        return {}
    parsed = parse_tool_arguments(call.raw_args or "{}", {})
    return parsed.args or {}


# -- the volatile block ----------------------------------------------------------------------


def _parts(
    scope: TurnScope, *, replayed: Sequence[Message], earlier: str | None = None
) -> list[Part]:
    """The parts of the first message, oldest context first and the request last."""
    context = scope.context
    records = scope.spec.memory == "records"
    parts: list[Part] = []
    if earlier:
        parts.append(Part("earlier_turns", earlier, False, PRIORITY["earlier_turns"]))
    if records:
        memory = context.memory_part(scope.memory, profile="agentic")
        if memory is not None:
            parts.append(memory)
    since = context.since_part(scope.memory)
    if since is not None:
        parts.append(since)
    parts.append(_project_part(scope.staging, context))
    outline = _outline(scope)
    editor = scope.request.editor
    if outline is not None:
        active = editor.path if editor is not None and editor.path.endswith(".tex") else None
        parts.append(
            context.outline_part(outline, active=active, replayed=replayed, cap=_OUTLINE_CAP)
        )
    proposal = _proposal_part(scope.staging)
    if proposal is not None:
        parts.append(proposal)
    diagnostics = context.diagnostics_part(
        scope.diagnostics,
        replayed=replayed,
        read=lambda path: _text(scope.staging, path),
        cap=_DIAGNOSTICS_CAP,
    )
    if diagnostics is not None:
        parts.append(diagnostics)
    if editor is not None:
        parts.append(_editor_part(scope.staging, editor, context))
    if scope.intent == "question":
        excerpt = _excerpt_part(scope, outline)
        if excerpt is not None:
            parts.append(excerpt)
        parts.append(Part("intent", QUESTION_NOTE, True, PRIORITY["request"]))
    parts.append(context.request_part(scope.request.message))
    return parts


def _element(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def _fitting(
    key: str, renderings: Sequence[str], context: ContextAssembler, cap: int | None
) -> Part:
    """A part whose first rendering within `cap` is its text, with the rest as shrinks."""
    first = 0
    if cap is not None:
        fits = [
            i
            for i, text in enumerate(renderings)
            if context.estimator.estimate(text, messages=0) <= cap
        ]
        first = fits[0] if fits else len(renderings) - 1
    rest = tuple(_constant(text) for text in renderings[first + 1 :])
    return Part(key, renderings[first], False, PRIORITY[key], rest)


def _constant(text: str) -> Callable[[], str]:
    return lambda: text


def _text(staging: Staging, path: str) -> str | None:
    try:
        return staging.read(path).text
    except ToolError:
        return None


def _outline(scope: TurnScope) -> Outline | None:
    if scope.staging.base.root_file is None:
        return None
    try:
        return scope.outline()
    except RootFileMissing:
        return None


def _project_part(staging: Staging, context: ContextAssembler) -> Part:
    base = staging.base
    assets = set(base.assets)
    sources = [path for path in staging.paths() if path not in assets]
    if base.root_file is None:
        head = "The project has no root file, so it is not compiled."
    else:
        head = f"Root file: {base.root_file}. Engine: {base.engine}."

    def listing(paths: Sequence[str], limit: int | None) -> str:
        shown = list(paths if limit is None else paths[:limit])
        more = len(paths) - len(shown)
        text = ", ".join(shown) if shown else "none"
        return text + (f", and {more} more" if more else "")

    full = [head, f"Source files: {listing(sources, None)}"]
    if base.assets:
        full.append(f"Other files (names only): {listing(base.assets, None)}")
    renderings = [
        _element("project", "\n".join(full)),
        _element("project", f"{head}\nSource files: {listing(sources, None)}"),
        _element("project", f"{head}\nSource files: {listing(sources, _LISTED_FILES)}"),
        _element("project", head),
    ]
    return _fitting("project", list(dict.fromkeys(renderings)), context, _PROJECT_CAP)


def _proposal_part(staging: Staging) -> Part | None:
    """The open Proposal, carried into this Turn: which files its undecided changes touch."""
    carried = [
        f.path for f in staging.files() if f.start != (f.base.text if f.base is not None else None)
    ]
    if not carried:
        return None
    text = (
        "A proposal from an earlier turn is still under review. Its undecided changes are "
        f"already in the text you see, in {', '.join(carried)}. Build on them; the author "
        "reviews the earlier and the new changes together."
    )
    return Part("proposal", _element("proposal", text), False, PRIORITY["proposal"])


def _line_map(old: str, new: str) -> Callable[[int], int]:
    """Maps a line of `old` to the matching line of `new` (the nearest one when it changed)."""
    if old == new:
        return lambda line: line
    old_lines, new_lines = old.split("\n"), new.split("\n")
    opcodes = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()

    def mapped(line: int) -> int:
        index = line - 1
        for tag, i1, i2, j1, j2 in opcodes:
            if i1 <= index < i2:
                if tag == "equal":
                    return j1 + (index - i1) + 1
                return min(j1 + (index - i1), max(j2 - 1, j1)) + 1
        return len(new_lines)

    return mapped


def _editor_part(staging: Staging, editor: EditorContext, context: ContextAssembler) -> Part:
    path = editor.path
    head = [f"Active file: {path}" + (" (with unsaved edits)" if editor.buffer is not None else "")]
    text = _text(staging, path)
    if text is None:
        return _fitting("editor", [_element("editor", head[0])], context, _EDITOR_CAP)
    base = staging.base.files.get(path)
    to_staged = _line_map(base.text, text) if base is not None else (lambda line: line)
    total = len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    marked: tuple[int, int] | None = None
    centre: int | None = None
    if editor.selection is not None:
        sel = editor.selection
        end = (
            sel.end_line - 1 if sel.end_col == 1 and sel.end_line > sel.start_line else sel.end_line
        )
        first, last = to_staged(sel.start_line), to_staged(max(end, sel.start_line))
        marked = (first, max(first, last))
        head.append(f"Selection: lines {marked[0]}-{marked[1]}")
    if editor.cursor_line is not None:
        centre = to_staged(editor.cursor_line)
        head.append(f"Cursor: line {centre}")
    renderings: list[str] = []
    if total and (marked is not None or centre is not None):
        lo, hi = marked if marked is not None else (centre or 1, centre or 1)
        lines = text.split("\n")
        for radius in (EDITOR_RADIUS, 5):
            first, last = max(1, lo - radius), min(total, hi + radius)
            width = len(str(last))
            numbered = [
                (" > " if marked is not None and marked[0] <= n <= marked[1] else "   ")
                + str(n).rjust(width)
                + "| "
                + lines[n - 1]
                for n in range(first, last + 1)
            ]
            renderings.append(_element("editor", "\n".join([*head, *numbered])))
    renderings.append(_element("editor", "\n".join(head)))
    return _fitting("editor", renderings, context, _EDITOR_CAP)


def _excerpt_part(scope: TurnScope, outline: Outline | None) -> Part | None:
    """The forced read of a question Turn (agent design §4.1), numbered, at most 9,000 tokens.

    The first source that applies: the selection; the section at the cursor; a
    heading or `\\label` the message names; the active file; the root file and
    the files it includes, in document order.
    """
    spans = _excerpt_spans(scope, outline)
    if not spans:
        return None
    views: list[str] = []
    for path, start, end in spans:
        text = _text(scope.staging, path)
        if text is not None:
            views.append(render_view(path, text, start, end, max_lines=_ALL_LINES).body)
    if not views:
        return None
    body = "\n\n".join(views)
    context = scope.context
    renderings = [
        _element("excerpt", _cut(body, cap, context))
        for cap in (EXCERPT_TOKENS, EXCERPT_TOKENS // 2, EXCERPT_TOKENS // 4)
    ]
    return _fitting("excerpt", list(dict.fromkeys(renderings)), context, EXCERPT_TOKENS)


def _excerpt_spans(
    scope: TurnScope, outline: Outline | None
) -> list[tuple[str, int | None, int | None]]:
    staging = scope.staging
    editor = scope.request.editor
    message = scope.request.message
    if editor is not None and _text(staging, editor.path) is not None:
        text = _text(staging, editor.path) or ""
        base = staging.base.files.get(editor.path)
        to_staged = _line_map(base.text, text) if base is not None else (lambda line: line)
        if editor.selection is not None:
            sel = editor.selection
            end = (
                sel.end_line - 1
                if sel.end_col == 1 and sel.end_line > sel.start_line
                else sel.end_line
            )
            return [(editor.path, to_staged(sel.start_line), to_staged(max(end, sel.start_line)))]
        if editor.cursor_line is not None and outline is not None:
            heading = outline.section_at(editor.path, to_staged(editor.cursor_line))
            if heading is not None:
                return [(heading.path, heading.line, heading.end_line)]
    if outline is not None:
        lowered = message.casefold()
        for heading in outline.headings:
            named = len(heading.title) >= 3 and heading.title.casefold() in lowered
            if named or (heading.label is not None and heading.label in message):
                return [(heading.path, heading.line, heading.end_line)]
        for key, label in outline.labels.items():
            if key in message:
                heading = outline.section_at(label.path, label.line)
                if heading is not None:
                    return [(heading.path, heading.line, heading.end_line)]
    if editor is not None and _text(staging, editor.path) is not None:
        return [(editor.path, None, None)]
    if outline is None:
        return []
    return [(path, None, None) for path in _document_order(outline)]


def _document_order(outline: Outline) -> list[str]:
    """The root file and the files it includes, depth first in the order they are named."""
    order: list[str] = []

    def visit(path: str) -> None:
        if path in order:
            return
        order.append(path)
        for target in outline.inputs.get(path, ()):
            visit(target)

    visit(outline.root_file)
    return order


def _cut(body: str, cap: int, context: ContextAssembler) -> str:
    if context.estimator.estimate(body, messages=0) <= cap:
        return body
    note = f"[The excerpt was cut at about {cap:,} tokens. Use view to read the rest.]"
    room = int(cap * context.estimator.chars_per_token) - len(note) - 8
    cut = body.rfind("\n", 0, room)
    return body[: cut if cut > 0 else room] + "\n" + note
