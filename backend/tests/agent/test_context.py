"""Context assembly and token budgets (agent design §2.2 `context`, §5)."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from prismtex.agent.context import (
    PRIORITY,
    PROFILE_PROMPTS,
    STUBBED_TOOL_RESULT,
    UNCHANGED_DIAGNOSTICS,
    UNCHANGED_OUTLINE,
    ContextAssembler,
    ContextOverflow,
    Part,
    PromptSet,
    SegmentKey,
    TokenBudget,
    TokenEstimator,
    margin_for,
    open_segment,
)
from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelSpec,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    catalog,
)
from prismtex.agent.threads import RegionRef, Segment, ThreadMemory, TurnRecord
from prismtex.agent.tools import ToolSpec
from prismtex.agent.types import ProfileName
from prismtex.agent.verify import DiagnosticView
from prismtex.outline import Outline, build_outline

QWEN = catalog.spec(catalog.OLLAMA_DEFAULT)
CLAUDE_LIKE = catalog.ANTHROPIC_LIKE
FAKE = catalog.FAKE_SPEC

MAIN = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Introduction}\\label{sec:intro}\n"
    "We study graphs.\n"
    "\\subsection{Background}\n"
    "Prior work.\n"
    "\\input{sections/method}\n"
    "\\section{Conclusion}\n"
    "We are done.\n"
    "\\bibliography{refs}\n"
    "\\end{document}\n"
)
METHOD = "\\section{Method}\nWe use a \\textbf{simple} model.\n"


def assembler(chars_per_token: float = 3.0) -> ContextAssembler:
    return ContextAssembler(PromptSet.load(), TokenEstimator(chars_per_token))


def outline_of(files: dict[str, str]) -> Outline:
    return build_outline(lambda path: files[path], list(files), "main.tex")


def user(text: str) -> Message:
    return Message("user", (TextBlock(text),))


def assistant(text: str) -> Message:
    return Message("assistant", (TextBlock(text),))


# -- pack ------------------------------------------------------------------------------


def test_pack_refuses_before_sending_when_the_request_cannot_fit() -> None:
    asm = ContextAssembler(PromptSet.load(), TokenEstimator(3.0))
    budget = TokenBudget(window=2048, output=1024, margin=512, cap=None)  # input = 512
    assert budget.input == 512
    with pytest.raises(ContextOverflow) as exc:
        asm.pack(
            [
                Part("request", "x" * 3000, required=True, priority=0),
                Part("memory", "m" * 600, required=False, priority=8),
            ],
            budget,
            fixed=100,
        )
    assert exc.value.limit == 512 and exc.value.needed > 512
    assert exc.value.breakdown["request"] == 1000
    packed = asm.pack(
        [
            Part("request", "x" * 300, required=True, priority=0),
            Part("memory", "m" * 2400, required=False, priority=8),
        ],
        budget,
        fixed=100,
    )
    assert packed.trimmed == ("memory",) and packed.tokens == 100 + 100 + 4


def test_trim_order_drops_memory_before_the_outline_and_never_the_request() -> None:
    assert PRIORITY["request"] < PRIORITY["region"] < PRIORITY["outline"] < PRIORITY["memory"]
    asm = assembler(chars_per_token=1.0)  # one character per token keeps the sums plain

    def parts() -> list[Part]:
        return [
            Part("outline", "O" * 200, False, PRIORITY["outline"], (lambda: "o" * 60,)),
            Part("region", "G" * 100, True, PRIORITY["region"], (lambda: "g" * 20,)),
            Part("memory", "M" * 300, False, PRIORITY["memory"], (lambda: "m" * 100,)),
            # A shrink offered for the request is never used.
            Part("request", "R" * 50, True, PRIORITY["request"], (lambda: "r",)),
        ]

    def budget(room: int) -> TokenBudget:
        return TokenBudget(window=room, output=0, margin=0, cap=None)

    untouched = asm.pack(parts(), budget(1_000), fixed=0)
    assert untouched.trimmed == () and untouched.tokens == 654
    assert untouched.text == "\n".join(["O" * 200, "G" * 100, "M" * 300, "R" * 50])

    # Memory shrinks first, then goes; the outline is untouched.
    packed = asm.pack(parts(), budget(400), fixed=0)
    assert packed.trimmed == ("memory",)
    assert dict(packed.parts) == {"outline": 200, "region": 100, "request": 50}
    assert packed.tokens == 354

    # Next the outline shrinks and goes; the required region is still whole.
    packed = asm.pack(parts(), budget(200), fixed=0)
    assert packed.trimmed == ("memory", "outline")
    assert dict(packed.parts) == {"region": 100, "request": 50}

    # Required parts shrink only once every optional part is gone.
    packed = asm.pack(parts(), budget(100), fixed=0)
    assert packed.trimmed == ("memory", "outline", "region")
    assert packed.text == "g" * 20 + "\n" + "R" * 50

    # With nothing left to shrink, the request is not touched: the call is refused.
    with pytest.raises(ContextOverflow) as exc:
        asm.pack(parts(), budget(60), fixed=0)
    assert (exc.value.needed, exc.value.limit) == (74, 60)
    assert exc.value.breakdown == {"fixed": 0, "region": 20, "request": 50, "overhead": 4}


def test_pack_rejects_duplicate_part_keys() -> None:
    with pytest.raises(ValueError, match="unique"):
        assembler().pack(
            [Part("request", "a", True, 0), Part("request", "b", True, 0)],
            TokenBudget(window=1000, output=0, margin=0, cap=None),
            fixed=0,
        )


# -- budgets and estimation -------------------------------------------------------------


def test_budget_for_spec_pins_the_window_and_bounds_the_margin() -> None:
    assert (margin_for(16_384), margin_for(40_000), margin_for(1_000_000)) == (512, 1_200, 2_048)
    guided = TokenBudget.for_spec(QWEN, output=1_756)  # a 1,000-token Region (agent design §5.2)
    assert (guided.window, guided.margin, guided.cap, guided.input) == (16_384, 512, None, 14_116)
    claude = TokenBudget.for_spec(CLAUDE_LIKE, output=16_000)
    assert (claude.window, claude.margin, claude.input) == (1_000_000, 2_048, 100_000)
    assert TokenBudget(window=1_000, output=900, margin=512, cap=None).input == 0


def test_estimator_calibrates_only_downward() -> None:
    est = TokenEstimator(3.0)
    assert est.estimate("x" * 300) == 100 + 4
    assert est.estimate("x" * 300, messages=0) == 100
    est.observe(1_000, 250)  # 4.0 characters per token: sparser, ignored
    assert est.chars_per_token == 3.0
    est.observe(1_000, 400)  # 2.5: denser, adopted
    assert est.chars_per_token == 2.5
    assert est.estimate("x" * 300) == 120 + 4
    est.observe(1_000, 200)
    est.observe(0, 10)
    est.observe(10, 0)
    assert est.chars_per_token == 2.5
    est.observe(10, 100)  # never below one character per token
    assert est.chars_per_token == 1.0
    with pytest.raises(ValueError):
        TokenEstimator(0)

    # The assembler calibrates from a whole call, net of the message overhead,
    # and ignores calls too small to say anything.
    asm = assembler()
    call = ModelCall(system="s" * 1_000, messages=(user("u" * 2_000),))
    asm.observe(call, 1_000 + 8)
    assert asm.estimator.chars_per_token == 3.0
    asm.observe(ModelCall(system="", messages=(user("tiny"),)), 100)
    assert asm.estimator.chars_per_token == 3.0
    asm.observe(call, 1_500 + 8)
    assert asm.estimator.chars_per_token == 2.0


def test_estimate_counts_system_tools_schema_and_every_message() -> None:
    asm = assembler(chars_per_token=1.0)
    tool = ToolSpec("view", "Show lines.", {"type": "object", "properties": {}}, read_only=True)
    bare = ModelCall(system="abc", messages=(user("hello"), assistant("hi")))
    assert asm.estimate(bare) == (3 + 4) + (5 + 4) + (2 + 4)
    assert asm.estimate(replace(bare, tools=(tool,))) > asm.estimate(bare)
    schema = replace(bare, response_schema={"type": "object"})
    assert asm.estimate(schema) > asm.estimate(bare)
    # A message with a native payload costs at least as much as that payload.
    native = Message("assistant", (TextBlock("hi"),), {"content": [{"thinking": "t" * 500}]})
    assert asm.estimate(ModelCall(system="", messages=(native,))) > 500


# -- fit ------------------------------------------------------------------------------------


def test_fit_uses_the_exact_count_near_the_limit_and_raises_before_sending() -> None:
    spec = replace(FAKE, context_window=4_096)
    limit = TokenBudget.for_spec(spec, 1_024).input
    assert limit == 4_096 - 1_024 - 512 == 2_560

    near = ModelCall(system="", messages=(user("x" * 7_000),), max_output_tokens=1_024)
    asm = assembler()
    assert asm.estimate(near) == 2_334 + 4
    assert asm.near_limit(near, spec)
    far = replace(near, messages=(user("x" * 3_000),))
    assert not asm.near_limit(far, spec)
    assert asm.fit(far, spec) is far

    # The exact count decides, whichever side of the limit the estimate is on.
    assert asm.fit(near, spec, exact=2_500) is near
    over = replace(near, messages=(user("x" * 9_000),))
    assert asm.estimate(over) > limit
    assert asm.fit(over, spec, exact=2_400) is over
    with pytest.raises(ContextOverflow) as exc:
        asm.fit(near, spec, exact=2_600)
    assert (exc.value.needed, exc.value.limit) == (2_600, 2_560)
    assert set(exc.value.breakdown) == {"system", "tools", "messages"}

    # Without an exact count the estimate decides, and nothing is cut under
    # transcript memory.
    with pytest.raises(ContextOverflow) as exc:
        assembler().fit(over, spec)
    assert exc.value.needed == assembler().estimate(over)
    transcript = replace(CLAUDE_LIKE, context_window=4_096, input_cap=None)
    results = _with_tool_results(3_000, 3_000, 3_000)
    with pytest.raises(ContextOverflow):
        assembler().fit(results, transcript)


def test_fit_calibrates_the_estimator_from_an_exact_count() -> None:
    asm = assembler()
    call = ModelCall(system="", messages=(user("x" * 6_000),))
    asm.fit(call, FAKE, exact=2_504)  # 6,000 characters in 2,500 tokens: 2.4 per token
    assert asm.estimator.chars_per_token == pytest.approx(2.4)


def _with_tool_results(*sizes: int) -> ModelCall:
    messages: list[Message] = [user("Fix the typo.")]
    for n, size in enumerate(sizes):
        messages.append(Message("assistant", (ToolUseBlock(f"c{n}", "view", {"path": "a"}),)))
        messages.append(Message("user", (ToolResultBlock(f"c{n}", "r" * size),)))
    return ModelCall(system="", messages=tuple(messages), max_output_tokens=1_024)


def test_fit_cuts_the_oldest_tool_results_to_stubs_under_records_memory() -> None:
    spec = replace(FAKE, context_window=3_200)  # limit 3,200 - 1,024 - 512 = 1,664
    call = _with_tool_results(2_400, 2_400, 600)  # about 1,826 estimated tokens
    fitted = assembler().fit(call, spec)
    assert fitted is not call
    contents = [
        b.content for m in fitted.messages for b in m.blocks if isinstance(b, ToolResultBlock)
    ]
    assert contents == [STUBBED_TOOL_RESULT, "r" * 2_400, "r" * 600]
    assert assembler().estimate(fitted) <= 1_664
    # The last message is never cut: when only it is too large, the call is refused.
    with pytest.raises(ContextOverflow):
        assembler().fit(_with_tool_results(9_000), spec)


# -- prompts ---------------------------------------------------------------------------------


def test_system_prompt_is_byte_identical_across_projects() -> None:
    first, second = assembler(), ContextAssembler(PromptSet.load(), TokenEstimator(2.0))
    cases: list[tuple[ProfileName, ModelSpec]] = [
        ("agentic", CLAUDE_LIKE),
        ("agentic", FAKE),
        ("guided", QWEN),
    ]
    systems = [first.system(profile, spec).encode("utf-8") for profile, spec in cases]
    # Packing two different Projects changes nothing in the system string.
    for files in (
        {"main.tex": MAIN, "sections/method.tex": METHOD},
        {"main.tex": "\\section{X}\n"},
    ):
        outline = outline_of(files)
        budget = TokenBudget.for_spec(QWEN, 1_024)
        first.pack(
            [first.outline_part(outline), first.request_part("Shorten it.")], budget, fixed=0
        )
        for (profile, spec), text in zip(cases, systems, strict=True):
            again = second.system(profile, spec)
            assert again.encode("utf-8") == text
            assert "sections/method" not in again and "Introduction" not in again
    assert systems[0] == systems[1] != systems[2]


def test_prompts_ship_for_every_profile_with_no_example_paths() -> None:
    prompts = PromptSet.load()
    asm = assembler()
    for profile, names in PROFILE_PROMPTS.items():
        for name in names:
            text = prompts.text(name)
            assert text and text == text.strip() and "\r" not in text
            assert not re.search(r"[\w-]+(?:/[\w-]+)*\.(?:tex|bib|sty|cls|md)\b", text), name
            spec = QWEN if profile == "guided" else FAKE
            assert asm.system(profile, spec, prompt=name) == text
    guided = asm.system("guided", QWEN)
    assert guided == prompts.text("guided_rewrite")
    assert "<<<REPLACEMENT" in guided and "REPLACEMENT>>>" in guided
    assert "\\documentclass" in guided and "<bib_keys>" in guided and "<focus>" in guided
    agentic = asm.system("agentic", FAKE)
    for tool in ("str_replace", "view", "ensure_package", "add_bib_entry", "check"):
        assert tool in agentic
    with pytest.raises(ValueError):
        asm.system("agentic", FAKE, prompt="router")
    no_tools = replace(FAKE, native_tools=False)
    with pytest.raises(ValueError):
        asm.system("agentic", no_tools)
    assert asm.system("guided", no_tools) == guided


def test_prompt_version_is_8_hex_per_profile_and_covers_the_tools() -> None:
    prompts = PromptSet.load()
    agentic = prompts.version("agentic")
    assert re.fullmatch(r"[0-9a-f]{8}", agentic)
    assert agentic == PromptSet.load().version("agentic")
    assert prompts.version("guided") != agentic
    tool = ToolSpec("view", "Show lines.", {"type": "object", "properties": {}}, read_only=True)
    with_tool = prompts.version("agentic", (tool,))
    assert with_tool != agentic
    assert prompts.version("agentic", (replace(tool, description="Show the lines."),)) != with_tool
    edited = PromptSet({**prompts.texts, "router": prompts.text("router") + " "})
    assert edited.version("agentic") == agentic and edited.version("guided") != prompts.version(
        "guided"
    )


# -- the volatile block ----------------------------------------------------------------------


def test_outline_map_lists_spans_labels_inputs_and_shrinks_to_top_level_then_the_active_file() -> (
    None
):
    outline = outline_of({"main.tex": MAIN, "sections/method.tex": METHOD})
    part = assembler().outline_part(outline, active="sections/method.tex")
    assert (part.key, part.required, part.priority) == ("outline", False, PRIORITY["outline"])
    assert part.text.startswith('<outline root="main.tex">\n') and part.text.endswith("</outline>")
    assert "main.tex:L3-L6 \\section{Introduction} \\label{sec:intro}" in part.text
    assert "  main.tex:L5-L6 \\subsection{Background}" in part.text
    assert "sections/method.tex:L1-L2 \\section{Method}" in part.text
    assert "main.tex includes sections/method.tex" in part.text
    assert "Bibliography: refs.bib" in part.text
    top, own = (shrink() for shrink in part.shrink)
    assert "Background" not in top and "Method" in top
    assert "Method" in own and "Introduction" not in own
    assert "Headings in other files: main.tex (3)" in own
    # A cap picks the first rendering that fits.
    capped = assembler().outline_part(outline, cap=assembler().estimator.estimate(top, messages=0))
    assert capped.text == top and len(capped.shrink) == 1


def test_unchanged_outline_and_diagnostics_render_as_one_line_under_transcript_memory() -> None:
    asm = assembler()
    files = {"main.tex": MAIN, "sections/method.tex": METHOD}
    outline = outline_of(files)
    diagnostics = [
        DiagnosticView(
            "main.tex",
            4,
            "error",
            "undefined_control_sequence",
            "Undefined control sequence",
            "We study graphs.",
        ),
        DiagnosticView("main.tex", 9, "warning", "overfull_hbox", "Overfull \\hbox", None),
    ]

    def block(replayed: tuple[Message, ...], current: Outline = outline) -> str:
        parts = [
            asm.outline_part(current, replayed=replayed),
            asm.diagnostics_part(diagnostics, replayed=replayed, read=files.get),
            asm.request_part("Shorten the introduction."),
        ]
        budget = TokenBudget.for_spec(CLAUDE_LIKE, 16_000)
        return asm.pack([p for p in parts if p is not None], budget, fixed=0).text

    first = block(())
    assert first.startswith('<outline root="main.tex">\n')
    assert "<diagnostics>\nKnown compile problems before this turn: 1 error, 1 warning." in first
    assert " > 4| We study graphs." in first and "   1| \\documentclass{article}" in first
    assert "main.tex:9: warning: Overfull \\hbox" in first

    # The next Turn of the segment replays the first one: both parts are one line.
    transcript = (user(first), assistant("Done."))
    second = block(transcript)
    assert second.split("\n")[:2] == [UNCHANGED_OUTLINE, UNCHANGED_DIAGNOSTICS]
    assert "<outline" not in second and "<diagnostics>" not in second

    # A Turn after that still compares with the last full rendering.
    later = (*transcript, user(second), assistant("Done again."))
    assert block(later).split("\n")[0] == UNCHANGED_OUTLINE

    # A changed Outline is sent in full; so is anything under records memory (nothing replayed).
    changed = outline_of(
        {"main.tex": MAIN.replace("Conclusion", "Summary"), "sections/method.tex": METHOD}
    )
    assert block(later, changed).startswith('<outline root="main.tex">\n')
    assert block(()).startswith('<outline root="main.tex">\n')

    # Changed and changed back: the model last saw the other map, so it is sent again.
    flipped = (*transcript, user(block(transcript, changed)), assistant("Ok."))
    assert block(flipped).startswith('<outline root="main.tex">\n')

    # Problems the model was shown are never left to stand once they are gone.
    gone = asm.diagnostics_part([], replayed=later)
    assert gone is not None and gone.text.split("\n")[1] == (
        "Known compile problems before this turn: 0 errors, 0 warnings."
    )
    assert asm.diagnostics_part([], replayed=(user(gone.text),)) is None
    assert asm.diagnostics_part([]) is None


def test_diagnostics_part_shrinks_and_falls_back_to_the_source_line() -> None:
    asm = assembler()
    assert asm.diagnostics_part([]) is None
    diagnostics = [
        DiagnosticView("a.tex", 3, "error", "k", "Missing $ inserted", "x_1"),
        DiagnosticView(None, None, "error", "k", "Emergency stop", None),
        DiagnosticView("a.tex", None, "warning", "k", "Citation undefined", None),
        DiagnosticView("a.tex", 8, "warning", "k", "Overfull", None),
    ]
    part = asm.diagnostics_part(diagnostics, limit=3)
    assert part is not None
    assert part.text.split("\n") == [
        "<diagnostics>",
        "Known compile problems before this turn: 2 errors, 2 warnings.",
        "a.tex:3: error: Missing $ inserted",
        " > x_1",
        "(no file): error: Emergency stop",
        "a.tex: warning: Citation undefined",
        "</diagnostics>",
    ]
    first_only, counts = (shrink() for shrink in part.shrink)
    assert "Emergency stop" not in first_only and "Missing $" in first_only
    assert counts.split("\n") == [
        "<diagnostics>",
        "Known compile problems before this turn: 2 errors, 2 warnings.",
        "</diagnostics>",
    ]


def test_memory_and_since_parts_come_from_the_thread() -> None:
    asm = assembler()
    empty = ThreadMemory("th", (), None, (), ())
    assert asm.memory_part(empty, profile="guided") is None
    assert asm.since_part(empty) is None
    records = tuple(_record(f"t{n}", f"Request {n}") for n in range(1, 5))
    memory = ThreadMemory("th", records, None, (), ())
    part = asm.memory_part(memory, profile="guided")
    assert part is not None and (part.key, part.required, part.priority) == ("memory", False, 8)
    assert part.text.count("\nT-") == 3
    assert part.shrink[0]().count("\nT-") == 1
    since = asm.since_part(memory, changed_files=["main.tex"])
    assert since is not None and since.priority == PRIORITY["since"]
    assert "The author edited main.tex." in since.text


def _record(turn_id: str, message: str, **fields: object) -> TurnRecord:
    base = TurnRecord(
        turn_id=turn_id,
        at="2026-11-28T10:00:00.000Z",
        status="completed",
        outcome="proposed",
        model="ollama:qwen3:4b-instruct-2507-q4_K_M",
        profile="guided",
        message=message,
        summary="Proposed 1 change in main.tex. Compiles with no new errors.",
        proposal_id=turn_id,
        targets=(RegionRef("main.tex", 9, 10),),
        added_text=None,
    )
    return replace(base, **fields)  # pyright: ignore[reportArgumentType]


# -- segments ----------------------------------------------------------------------------------


def _memory(spec: ModelSpec, *, transcript_chars: int, records: int = 1) -> ThreadMemory:
    segment = Segment("s1", spec.ref, "agentic", "v1", spec.effort, "new_thread", None)
    transcript = (user("u" * transcript_chars), assistant("ok"))
    turns = tuple(
        _record(f"t{n}", "Edit", model=spec.ref, profile="agentic") for n in range(records)
    )
    return ThreadMemory("th", turns, segment, transcript, ())


def test_plan_segment_starts_new_on_a_key_change_and_above_70_percent() -> None:
    asm = assembler()
    spec = CLAUDE_LIKE  # input cap 100,000: a segment rolls over above 70,000 tokens
    memory = _memory(spec, transcript_chars=30_000)  # 10,000 + 4 and 1 + 4 tokens
    key = SegmentKey.of(spec, "agentic", "v1")
    assert key == SegmentKey(spec.ref, "agentic", "v1", "medium")
    assert asm.plan_segment(memory, 1_000, spec, key) == "continue"

    for changed in (
        replace(key, model="anthropic:claude-sonnet-5-5"),
        replace(key, profile="guided"),
        replace(key, prompt_version="v2"),
        replace(key, effort="high"),
    ):
        assert asm.plan_segment(memory, 1_000, spec, changed) == "new"
    assert asm.plan_segment(replace(memory, segment=None), 1_000, spec, key) == "new"

    used = 10_004 + 5
    assert asm.plan_segment(memory, 70_000 - used, spec, key) == "continue"
    assert asm.plan_segment(memory, 70_000 - used + 1, spec, key) == "new"
    with pytest.raises(ValueError):
        asm.plan_segment(memory, 0, QWEN, key)


def test_open_segment_names_why_and_opens_with_earlier_turns() -> None:
    spec = CLAUDE_LIKE
    key = SegmentKey.of(spec, "agentic", "v1")
    fresh = open_segment(ThreadMemory("th", (), None, (), ()), key, segment_id="s0")
    assert (fresh.segment_id, fresh.opened_by, fresh.summary) == ("s0", "new_thread", None)
    memory = _memory(spec, transcript_chars=10)
    assert open_segment(memory, key).opened_by == "rollover"
    assert open_segment(memory, replace(key, prompt_version="v2")).opened_by == "prompt_version"
    assert open_segment(memory, replace(key, effort="low")).opened_by == "model_switch"
    after_records = open_segment(replace(memory, segment=None), key)
    assert after_records.opened_by == "model_switch"
    assert after_records.summary is not None and after_records.summary.startswith("<earlier_turns>")
    assert len(open_segment(memory, key).segment_id) == 32
