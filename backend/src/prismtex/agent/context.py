"""Context assembly and token budgets (agent design §2.2 `context`, §5).

Every model call must fit in `input = min(window - output - margin, cap)`; the
engine refuses a call that does not, before anything is sent (invariant I8).

- **Estimation.** `ceil(chars / chars_per_token)` plus 4 per message, seeded at
  the model's catalog value and calibrated only downward from reported sizes.
- **Stable prefix.** `system` is byte-identical per Profile and prompt version
  and holds no Project data. Prompt text lives in `profiles/prompts/*.md`.
- **Volatile block.** The first user message of a Turn is packed from tagged
  parts. When it does not fit, the part with the highest `priority` number is
  shrunk or dropped first; required parts shrink only after every optional
  part is gone, and the request is never trimmed.
- **Unchanged parts.** When the Outline map or the Diagnostics equal what the
  replayed transcript last showed, they render as one line.
- **Segments.** Under transcript memory a new segment starts when the model,
  Profile, prompt version or effort changes, or when the transcript plus the
  new block would pass 70% of the input cap.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any, Final, Literal

from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelSpec,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from prismtex.agent.threads import (
    Segment,
    SegmentOpening,
    ThreadMemory,
    earlier_turns,
    render_records,
    render_since,
)
from prismtex.agent.tools import ToolSpec
from prismtex.agent.types import ProfileName
from prismtex.agent.verify import DiagnosticView
from prismtex.outline import Heading, Outline

__all__ = [
    "MESSAGE_OVERHEAD",
    "NEAR_LIMIT",
    "PRIORITY",
    "PROFILE_PROMPTS",
    "PROMPT_FILES",
    "ROLLOVER",
    "STUBBED_TOOL_RESULT",
    "UNCHANGED_DIAGNOSTICS",
    "UNCHANGED_OUTLINE",
    "ContextAssembler",
    "ContextOverflow",
    "Packed",
    "Part",
    "PromptName",
    "PromptSet",
    "SegmentKey",
    "TokenBudget",
    "TokenEstimator",
    "margin_for",
    "open_segment",
]

MESSAGE_OVERHEAD: Final = 4  # estimated tokens per message, on top of its text
NEAR_LIMIT: Final = 0.8  # from this share of the limit on, the exact count decides
ROLLOVER: Final = 0.7  # a segment rolls over above this share of the input cap
_MARGIN_MIN: Final = 512
_MARGIN_MAX: Final = 2_048
_MARGIN_SHARE: Final = 0.03
_MIN_CHARS_PER_TOKEN: Final = 1.0
_MIN_OBSERVED_CHARS: Final = 1_000  # smaller calls are mostly template overhead
_VERSION_HEX: Final = 8

# Part priorities, lowest first: a lower number survives longer (agent design §5.1).
PRIORITY: Final[Mapping[str, int]] = MappingProxyType(
    {
        "request": 0,
        "region": 1,
        "selection": 1,
        "before": 1,
        "after": 1,
        "focus": 1,
        "bib_keys": 1,
        "editor": 2,
        "excerpt": 3,
        "project": 4,
        "outline": 4,
        "diagnostics": 5,
        "proposal": 6,
        "since": 7,
        "memory": 8,
        "earlier_turns": 8,
    }
)

UNCHANGED_OUTLINE: Final = "Outline unchanged since your last turn."
UNCHANGED_DIAGNOSTICS: Final = "Diagnostics unchanged since your last turn."
STUBBED_TOOL_RESULT: Final = (
    "[This tool result was removed to fit the model's context. Call the tool again if you need it.]"
)

PromptName = Literal["agentic", "guided_rewrite", "guided_answer", "router"]
PROMPT_FILES: Final[Mapping[PromptName, str]] = MappingProxyType(
    {
        "agentic": "agentic.md",
        "guided_rewrite": "guided_rewrite.md",
        "guided_answer": "guided_answer.md",
        "router": "router.md",
    }
)
# The prompts of each Profile; the first is its default system string.
PROFILE_PROMPTS: Final[Mapping[ProfileName, tuple[PromptName, ...]]] = MappingProxyType(
    {"agentic": ("agentic",), "guided": ("guided_rewrite", "guided_answer", "router")}
)

_COMMANDS: Final[Mapping[int, str]] = MappingProxyType(
    {
        -1: "part",
        0: "chapter",
        1: "section",
        2: "subsection",
        3: "subsubsection",
        4: "paragraph",
        5: "subparagraph",
    }
)


# -- budgets and estimation ------------------------------------------------------------


def margin_for(window: int) -> int:
    """The safety margin of a window: 3% of it, at least 512 and at most 2,048 tokens."""
    return min(_MARGIN_MAX, max(_MARGIN_MIN, math.ceil(window * _MARGIN_SHARE)))


@dataclass(frozen=True)
class TokenBudget:
    """The prompt room of one call: `input = min(window - output - margin, cap)`."""

    window: int
    output: int
    margin: int
    cap: int | None

    @property
    def input(self) -> int:
        room = max(0, self.window - self.output - self.margin)
        return room if self.cap is None else min(room, self.cap)

    @classmethod
    def for_spec(cls, spec: ModelSpec, output: int) -> TokenBudget:
        """The budget of a call to `spec` that reserves `output` tokens.

        The window is the pinned `num_ctx` when there is one (Ollama), so the
        server never truncates the prompt.
        """
        window = spec.num_ctx if spec.num_ctx is not None else spec.context_window
        return cls(window=window, output=output, margin=margin_for(window), cap=spec.input_cap)


class TokenEstimator:
    """Estimates tokens from characters; calibration only ever lowers `chars_per_token`.

    Share one estimator per model so its calibration carries across Turns.
    """

    def __init__(self, chars_per_token: float) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = float(chars_per_token)

    @property
    def chars_per_token(self) -> float:
        return self._chars_per_token

    def estimate(self, text: str, *, messages: int = 1) -> int:
        """`ceil(len(text) / chars_per_token)` plus 4 for each of `messages` messages."""
        return math.ceil(len(text) / self._chars_per_token) + MESSAGE_OVERHEAD * messages

    def observe(self, chars: int, reported: int) -> None:
        """Calibrate from `chars` characters the Provider counted as `reported` tokens.

        Only a denser observation lowers the ratio, never below 1 character
        per token, so later estimates stay on the safe side.
        """
        if chars <= 0 or reported <= 0:
            return
        observed = max(_MIN_CHARS_PER_TOKEN, chars / reported)
        if observed < self._chars_per_token:
            self._chars_per_token = observed


# -- prompts -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptSet:
    """The prompt texts by name (`PROMPT_FILES`), as shipped in `profiles/prompts`."""

    texts: Mapping[str, str]

    @classmethod
    def load(cls) -> PromptSet:
        return cls(_shipped_prompts())

    def text(self, name: PromptName) -> str:
        return self.texts[name]

    def version(self, profile: ProfileName, tools: Sequence[ToolSpec] = ()) -> str:
        """8 hex digits of SHA-256 over the Profile's prompt texts and the tool array.

        The tools count in the order given, which is the order they are sent;
        so any change to what the model sees as its fixed prefix changes the
        version, and Traces and eval cassettes can key on it.
        """
        payload = {
            "profile": profile,
            "prompts": {name: self.texts[name] for name in PROFILE_PROMPTS[profile]},
            "tools": [_tool_json(tool) for tool in tools],
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:_VERSION_HEX]


@cache
def _shipped_prompts() -> Mapping[str, str]:
    folder = resources.files("prismtex.agent") / "profiles" / "prompts"
    return MappingProxyType(
        {
            name: (folder / file).read_text(encoding="utf-8").replace("\r\n", "\n").strip()
            for name, file in PROMPT_FILES.items()
        }
    )


# -- records -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """One tagged part of a packed message.

    `priority`: a lower number survives longer (see `PRIORITY`). `shrink`
    holds smaller renderings, tried in order before an optional part is
    dropped.
    """

    key: str
    text: str
    required: bool
    priority: int
    shrink: tuple[Callable[[], str], ...] = ()


@dataclass(frozen=True)
class Packed:
    """A packed message: its text and its estimated size.

    `tokens` counts the whole call: `fixed`, every kept part and the message
    overhead. `parts` gives the tokens of each kept part; `trimmed` names the
    parts that were shrunk or dropped (a dropped part is absent from `parts`).
    """

    text: str
    tokens: int
    parts: Mapping[str, int]
    trimmed: tuple[str, ...]


@dataclass(frozen=True)
class SegmentKey:
    """What must stay fixed inside a transcript segment. `model` is the model ref."""

    model: str
    profile: ProfileName
    prompt_version: str
    effort: str | None

    @classmethod
    def of(cls, spec: ModelSpec, profile: ProfileName, prompt_version: str) -> SegmentKey:
        return cls(spec.ref, profile, prompt_version, spec.effort)

    def matches(self, segment: Segment) -> bool:
        return (self.model, self.profile, self.prompt_version, self.effort) == (
            segment.model,
            segment.profile,
            segment.prompt_version,
            segment.effort,
        )


class ContextOverflow(Exception):
    """A call cannot fit: `needed` tokens against a `limit`, with a per-part breakdown."""

    needed: int
    limit: int
    breakdown: dict[str, int]

    def __init__(self, needed: int, limit: int, breakdown: Mapping[str, int]) -> None:
        self.needed = needed
        self.limit = limit
        self.breakdown = dict(breakdown)
        super().__init__(
            f"The request needs about {needed} tokens, but the model's context allows {limit}."
        )


@dataclass
class _Slot:
    part: Part
    text: str
    tokens: int
    next_shrink: int = 0
    alive: bool = True


# -- the assembler -------------------------------------------------------------------------


class ContextAssembler:
    """Builds and checks what a Step sends (agent design §2.2 `context`)."""

    def __init__(self, prompts: PromptSet, estimator: TokenEstimator) -> None:
        self._prompts = prompts
        self._estimator = estimator

    @property
    def prompts(self) -> PromptSet:
        return self._prompts

    @property
    def estimator(self) -> TokenEstimator:
        return self._estimator

    # -- the stable prefix ---------------------------------------------------------------

    def system(
        self, profile: ProfileName, spec: ModelSpec, *, prompt: PromptName | None = None
    ) -> str:
        """The system string: byte-identical per (Profile, prompt version), no Project data.

        `prompt` picks one of the Profile's prompts (`PROFILE_PROMPTS`); the
        default is its first. The agentic Profile needs a model with native
        tool calling.
        """
        names = PROFILE_PROMPTS[profile]
        name = names[0] if prompt is None else prompt
        if name not in names:
            raise ValueError(f"the {profile} Profile has no {name} prompt")
        if profile == "agentic" and not spec.native_tools:
            raise ValueError("the agentic Profile needs a model with native tool calling")
        return self._prompts.text(name)

    # -- packing ---------------------------------------------------------------------------

    def pack(self, parts: Sequence[Part], budget: TokenBudget, *, fixed: int) -> Packed:
        """Pack `parts` into one message that fits `budget.input`, or raise ContextOverflow.

        `fixed` is the estimated size of the rest of the call (system, tools,
        earlier messages). Parts keep their order; empty parts are left out.
        """
        keys = [part.key for part in parts]
        if len(set(keys)) != len(keys):
            raise ValueError("part keys must be unique")
        slots = [_Slot(part, part.text, self._tokens(part.text)) for part in parts]
        trimmed: list[str] = []

        def total() -> int:
            return fixed + sum(s.tokens for s in slots if s.alive) + MESSAGE_OVERHEAD

        while total() > budget.input:
            slot = _pick(s for s in slots if s.alive and not s.part.required)
            if slot is None:
                slot = _pick(
                    s
                    for s in slots
                    if s.alive and s.part.key != "request" and s.next_shrink < len(s.part.shrink)
                )
            if slot is None:
                kept = {s.part.key: s.tokens for s in slots if s.alive}
                breakdown = {"fixed": fixed, **kept, "overhead": MESSAGE_OVERHEAD}
                raise ContextOverflow(total(), budget.input, breakdown)
            if slot.next_shrink < len(slot.part.shrink):
                slot.text = slot.part.shrink[slot.next_shrink]()
                slot.tokens = self._tokens(slot.text)
                slot.next_shrink += 1
            else:
                slot.alive = False
            if slot.part.key not in trimmed:
                trimmed.append(slot.part.key)
        kept_slots = [s for s in slots if s.alive]
        return Packed(
            text="\n".join(s.text for s in kept_slots if s.text),
            tokens=total(),
            parts=MappingProxyType({s.part.key: s.tokens for s in kept_slots}),
            trimmed=tuple(trimmed),
        )

    # -- whole calls -------------------------------------------------------------------------

    def estimate(self, call: ModelCall) -> int:
        """The estimated prompt size of `call`: system, tools, response schema and messages."""
        return sum(self._call_parts(call).values())

    def breakdown(self, call: ModelCall) -> dict[str, int]:
        """The estimated tokens of each part of `call`: system, tools, messages, schema."""
        return self._call_parts(call)

    def near_limit(self, call: ModelCall, spec: ModelSpec) -> bool:
        """True when the estimate is within 20% of the limit, where an exact count decides."""
        limit = TokenBudget.for_spec(spec, call.max_output_tokens).input
        return self.estimate(call) >= NEAR_LIMIT * limit

    def observe(self, call: ModelCall, reported: int) -> None:
        """Calibrate the estimator from the prompt size a Provider reported for `call`."""
        chars = _call_chars(call)
        overhead = MESSAGE_OVERHEAD * (len(call.messages) + (1 if call.system else 0))
        if chars >= _MIN_OBSERVED_CHARS and reported > overhead:
            self._estimator.observe(chars, reported - overhead)

    def fit(self, call: ModelCall, spec: ModelSpec, *, exact: int | None = None) -> ModelCall:
        """`call` if it fits the model's input limit, else raise ContextOverflow (I8).

        `exact` is the Provider's own count (`ModelPort.count_tokens`), passed
        when the estimate is near the limit; it decides and calibrates the
        estimator. Without it, under records memory, the oldest tool results
        of the Turn are cut to one-line stubs until the call fits (agent design
        §5.2); the last message is never changed, and nothing is ever cut
        under transcript memory. The call is returned as is when it fits.
        """
        limit = TokenBudget.for_spec(spec, call.max_output_tokens).input
        if exact is not None:
            self.observe(call, exact)
            if exact <= limit:
                return call
            raise ContextOverflow(exact, limit, self._call_parts(call))
        needed = self.estimate(call)
        if needed <= limit:
            return call
        if spec.memory == "records":
            call, needed = self._stub_tool_results(call, limit)
            if needed <= limit:
                return call
        raise ContextOverflow(needed, limit, self._call_parts(call))

    # -- segments (transcript memory) ----------------------------------------------------------

    def plan_segment(
        self, memory: ThreadMemory, block_tokens: int, spec: ModelSpec, key: SegmentKey
    ) -> Literal["continue", "new"]:
        """Whether the Turn continues the Thread's segment or opens a new one (§5.4).

        "new" when there is no segment, when the model, Profile, prompt version
        or effort differs from the segment's, or when the estimated transcript
        plus the new volatile block (`block_tokens`) exceeds 70% of the input
        cap. Only transcript memory has segments.
        """
        if spec.memory != "transcript":
            raise ValueError("segments exist only under transcript memory")
        if memory.segment is None or not key.matches(memory.segment):
            return "new"
        cap = TokenBudget.for_spec(spec, spec.max_output).input
        used = sum(self._estimator.estimate(_message_text(m)) for m in memory.transcript)
        return "new" if used + block_tokens > ROLLOVER * cap else "continue"

    # -- parts of the volatile block -----------------------------------------------------------

    def request_part(self, message: str) -> Part:
        """The request: required and never trimmed."""
        return Part("request", _element("request", message), True, PRIORITY["request"])

    def outline_part(
        self,
        outline: Outline,
        *,
        active: str | None = None,
        replayed: Sequence[Message] = (),
        cap: int | None = None,
    ) -> Part:
        """The Outline map; one line when `replayed` last showed the same map.

        The map lists headings as `file:Lstart-Lend` with labels, the `\\input`
        graph, the bibliography files and the files the Root file never
        includes. It shrinks to the top-level sections, then to the headings
        of `active` (default: the Root file). `cap` picks the first rendering
        within that many tokens. Pass as `replayed` the transcript the call
        replays (transcript memory); under records memory pass nothing.
        """
        renderings = _outline_renderings(outline, active or outline.root_file)
        return self._part("outline", renderings, replayed, UNCHANGED_OUTLINE, cap)

    def diagnostics_part(
        self,
        diagnostics: Sequence[DiagnosticView],
        *,
        replayed: Sequence[Message] = (),
        read: Callable[[str], str | None] | None = None,
        limit: int = 3,
        cap: int | None = None,
    ) -> Part | None:
        """The Baseline Diagnostics: counts, then the first problem and up to `limit` in all.

        The first problem shows ±3 numbered lines when `read` returns its
        file's LF text, else its source line. One line when `replayed` last
        showed the same rendering. With no Diagnostics it is None, unless
        `replayed` last showed some: then it says there are none now.
        """
        if not diagnostics:
            last = _last_shown(replayed, "diagnostics") if replayed else None
            none = _element("diagnostics", _diagnostic_counts(0, 0))
            if last is None or last == none:
                return None
            return Part("diagnostics", none, False, PRIORITY["diagnostics"])
        renderings = _diagnostic_renderings(diagnostics, read, max(1, limit))
        return self._part("diagnostics", renderings, replayed, UNCHANGED_DIAGNOSTICS, cap)

    def memory_part(
        self, memory: ThreadMemory, *, profile: ProfileName, cap: int | None = None
    ) -> Part | None:
        """Records memory as `<memory>`; it shrinks to the last Turn. None without records."""
        full = render_records(memory, profile=profile)
        if not full:
            return None
        renderings = [full]
        if len(memory.records) > 1:
            renderings.append(render_records(memory, profile=profile, limit=1))
        return self._part("memory", renderings, (), None, cap)

    def since_part(self, memory: ThreadMemory, *, changed_files: Sequence[str] = ()) -> Part | None:
        """The "since your last Turn" note, or None when there is nothing to say."""
        text = render_since(memory, changed_files=changed_files)
        return Part("since", text, False, PRIORITY["since"]) if text else None

    # -- internals -------------------------------------------------------------------------

    def _tokens(self, text: str) -> int:
        return self._estimator.estimate(text, messages=0)

    def _part(
        self,
        key: str,
        renderings: Sequence[str],
        replayed: Sequence[Message],
        unchanged: str | None,
        cap: int | None,
    ) -> Part:
        first = 0
        if cap is not None:
            fitting = [i for i, text in enumerate(renderings) if self._tokens(text) <= cap]
            first = fitting[0] if fitting else len(renderings) - 1
        text = renderings[first]
        if unchanged is not None and replayed and _last_shown(replayed, key) == text:
            return Part(key, unchanged, False, PRIORITY[key])
        rest = tuple(_constant(r) for r in renderings[first + 1 :])
        return Part(key, text, False, PRIORITY[key], rest)

    def _call_parts(self, call: ModelCall) -> dict[str, int]:
        est = self._estimator
        parts = {
            "system": est.estimate(call.system) if call.system else 0,
            "tools": est.estimate(_tools_text(call.tools), messages=0) if call.tools else 0,
            "messages": sum(est.estimate(_message_text(m)) for m in call.messages),
        }
        if call.response_schema is not None:
            parts["response_schema"] = est.estimate(_canonical(call.response_schema), messages=0)
        return parts

    def _stub_tool_results(self, call: ModelCall, limit: int) -> tuple[ModelCall, int]:
        messages = list(call.messages)
        needed = self.estimate(call)
        for index in range(len(messages) - 1):
            message = messages[index]
            if message.role != "user" or message.native is not None:
                continue
            blocks = tuple(
                replace(b, content=STUBBED_TOOL_RESULT)
                if isinstance(b, ToolResultBlock) and len(b.content) > len(STUBBED_TOOL_RESULT)
                else b
                for b in message.blocks
            )
            if blocks == message.blocks:
                continue
            messages[index] = replace(message, blocks=blocks)
            call = replace(call, messages=tuple(messages))
            needed = self.estimate(call)
            if needed <= limit:
                break
        return call, needed


def open_segment(
    memory: ThreadMemory, key: SegmentKey, *, segment_id: str | None = None
) -> Segment:
    """The Segment a Turn opens when `plan_segment` says "new".

    `opened_by` says why, from what changed; `summary` is the
    `<earlier_turns>` text the segment's first message opens with, or None
    when the Thread has no Turns yet.
    """
    previous = memory.segment
    opened_by: SegmentOpening
    if not memory.records:
        opened_by = "new_thread"
    elif previous is None or (key.model, key.profile, key.effort) != (
        previous.model,
        previous.profile,
        previous.effort,
    ):
        opened_by = "model_switch"
    elif key.prompt_version != previous.prompt_version:
        opened_by = "prompt_version"
    else:
        opened_by = "rollover"
    return Segment(
        segment_id=segment_id or uuid.uuid4().hex,
        model=key.model,
        profile=key.profile,
        prompt_version=key.prompt_version,
        effort=key.effort,
        opened_by=opened_by,
        summary=earlier_turns(memory.records, memory.resolutions) or None,
    )


# -- rendering -------------------------------------------------------------------------------


def _element(tag: str, body: str, **attrs: str) -> str:
    head = "".join(f' {name}="{value}"' for name, value in attrs.items())
    return f"<{tag}{head}>\n{body}\n</{tag}>"


def _last_shown(replayed: Sequence[Message], tag: str) -> str | None:
    """The last full `<tag>` element in the replayed user messages, if any."""
    pattern = re.compile(rf"^<{tag}(?: [^>\n]*)?>\n.*?\n</{tag}>$", re.MULTILINE | re.DOTALL)
    for message in reversed(replayed):
        if message.role != "user":
            continue
        for block in reversed(message.blocks):
            if isinstance(block, TextBlock):
                found = pattern.findall(block.text)
                if found:
                    return found[-1]
    return None


def _outline_renderings(outline: Outline, active: str) -> list[str]:
    attrs = {"root": outline.root_file}
    extras: list[str] = []
    for source, targets in outline.inputs.items():
        if targets:
            extras.append(f"{source} includes {', '.join(targets)}")
    if outline.bib_files:
        extras.append(f"Bibliography: {', '.join(outline.bib_files)}")
    if outline.unincluded:
        extras.append(f"Not included by the root file: {', '.join(outline.unincluded)}")

    full = _heading_lines(outline.headings) + extras
    renderings = [_element("outline", "\n".join(full), **attrs)]
    if outline.headings:
        depth = max(min(h.level for h in outline.headings), 0)
        top = [h for h in outline.headings if h.level <= depth]
        if len(top) < len(outline.headings):
            renderings.append(_element("outline", "\n".join(_heading_lines(top) + extras), **attrs))
        own = [h for h in outline.headings if h.path == active]
        others: dict[str, int] = {}
        for h in outline.headings:
            if h.path != active:
                others[h.path] = others.get(h.path, 0) + 1
        lines = _heading_lines(own)
        if others:
            counted = ", ".join(f"{path} ({n})" for path, n in others.items())
            lines.append(f"Headings in other files: {counted}")
        small = _element("outline", "\n".join(lines), **attrs)
        if small != renderings[-1]:
            renderings.append(small)
    return renderings


def _heading_lines(headings: Sequence[Heading]) -> list[str]:
    if not headings:
        return ["No headings."]
    base = min(h.level for h in headings)
    lines: list[str] = []
    for h in headings:
        command = _COMMANDS.get(h.level, "section")
        label = f" \\label{{{h.label}}}" if h.label else ""
        indent = "  " * (h.level - base)
        lines.append(f"{indent}{h.path}:L{h.line}-L{h.end_line} \\{command}{{{h.title}}}{label}")
    return lines


def _diagnostic_renderings(
    diagnostics: Sequence[DiagnosticView], read: Callable[[str], str | None] | None, limit: int
) -> list[str]:
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    infos = [d for d in diagnostics if d.severity == "info"]
    ordered = errors + warnings + infos
    head = _diagnostic_counts(len(errors), len(warnings))
    first = [_diagnostic_line(ordered[0]), *_context_lines(ordered[0], read)]
    rest = [_diagnostic_line(d) for d in ordered[1:limit]]
    renderings = [_element("diagnostics", "\n".join([head, *first, *rest]))]
    if rest:
        renderings.append(_element("diagnostics", "\n".join([head, *first])))
    renderings.append(_element("diagnostics", head))
    return renderings


def _diagnostic_counts(errors: int, warnings: int) -> str:
    return (
        "Known compile problems before this turn: "
        f"{_count(errors, 'error')}, {_count(warnings, 'warning')}."
    )


def _diagnostic_line(d: DiagnosticView) -> str:
    if d.file is None:
        where = "(no file)"
    elif d.line is None:
        where = d.file
    else:
        where = f"{d.file}:{d.line}"
    return f"{where}: {d.severity}: {' '.join(d.message.split())}"


def _context_lines(d: DiagnosticView, read: Callable[[str], str | None] | None) -> list[str]:
    """±3 numbered lines around the problem, the problem line marked with ">"."""
    text = read(d.file) if read is not None and d.file is not None and d.line else None
    if text is not None and d.line is not None:
        lines = text.split("\n")
        if 1 <= d.line <= len(lines):
            out: list[str] = []
            for n in range(max(1, d.line - 3), min(len(lines), d.line + 3) + 1):
                mark = " > " if n == d.line else "   "
                out.append(f"{mark}{n}| {lines[n - 1]}")
            return out
    return [f" > {d.excerpt}"] if d.excerpt else []


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# -- sizes -------------------------------------------------------------------------------------


def _pick(slots: Iterable[_Slot]) -> _Slot | None:
    """The slot with the highest priority number; the later one on a tie."""
    best: _Slot | None = None
    for slot in slots:
        if best is None or slot.part.priority >= best.part.priority:
            best = slot
    return best


def _constant(text: str) -> Callable[[], str]:
    return lambda: text


def _message_text(message: Message) -> str:
    """The text a message costs: its native payload when it has one, else its blocks."""
    if message.native is not None:
        return _canonical(message.native)
    pieces: list[str] = []
    for block in message.blocks:
        if isinstance(block, TextBlock):
            pieces.append(block.text)
        elif isinstance(block, ToolUseBlock):
            pieces.append(block.name + _canonical(block.input))
        else:
            pieces.append(block.content)
    return "".join(pieces)


def _call_chars(call: ModelCall) -> int:
    chars = len(call.system) + sum(len(_message_text(m)) for m in call.messages)
    if call.tools:
        chars += len(_tools_text(call.tools))
    if call.response_schema is not None:
        chars += len(_canonical(call.response_schema))
    return chars


def _tools_text(tools: Sequence[ToolSpec]) -> str:
    return _canonical([_tool_json(tool) for tool in tools])


def _tool_json(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": _plain(tool.input_schema),
    }


def _canonical(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plain(value: Any) -> Any:
    """`value` with every Mapping a dict and every tuple a list, ready for JSON."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value
