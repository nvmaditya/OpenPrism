"""The agentic Profile: first message, tool loop, recovery and repair (agent design §4.2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from prismtex.agent.context import PromptSet, SegmentKey
from prismtex.agent.events import (
    MessageStartedEvent,
    NoticeEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from prismtex.agent.profiles.agentic import QUESTION_NOTE, AgenticProfile, repair_message
from prismtex.agent.providers import Message, TextBlock, ToolResultBlock, ToolUseBlock
from prismtex.agent.providers.catalog import RECOVER_TEXT_TOOL_CALLS
from prismtex.agent.providers.fake import (
    FakeModel,
    anthropic_like,
    fake_spec,
    refuse,
    say,
    step,
    tool,
    truncate,
)
from prismtex.agent.threads import Segment, ThreadMemory, TurnRecord
from prismtex.agent.tools import AGENTIC_TOOLS, READ_ONLY_TOOLS, STOP_HINT, TRUNCATED_ARGS
from prismtex.agent.turn import ProfileResult, RepairFeedback
from prismtex.agent.types import EditorContext, Selection
from prismtex.agent.verify import DiagnosticView
from prismtex.workspace import FileVersion, ProjectStore

Project = tuple[ProjectStore, str]
Put = Callable[[ProjectStore, str, str, str], FileVersion]
MakeScope = Callable[..., Any]

BLANK = "\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}\n"
PAPER = (
    "\\documentclass{article}\n"  # 1
    "\\begin{document}\n"  # 2
    "\\section{Introduction}\\label{sec:intro}\n"  # 3
    "Graph networks learn from structured data.\n"  # 4
    "\\section{Method}\n"  # 5
    "We aggregate neighbour features.\n"  # 6
    "\\end{document}\n"  # 7
)
FIX = '{"path": "main.tex", "old_str": "Hello", "new_str": "Hi"}'


def first_text(message: Message) -> str:
    block = message.blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


def setup(project: Project, put: Put, text: str = BLANK) -> Project:
    store, pid = project
    put(store, pid, "main.tex", text)
    return store, pid


@pytest.mark.anyio
async def test_parallel_tool_calls_apply_in_order_without_lost_edits(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put, PAPER)
    model = FakeModel(
        [
            step(
                say("Changing both."),
                tool("str_replace", '{"path": "main.tex", "old_str": "Graph", "new_str": "Deep"}'),
                tool("str_replace", '{"path": "main.tex", "old_str": "We sum", "new_str": "x"}'),
                tool("str_replace", '{"path": "main.tex", "old_str": "We", "new_str": "One"}'),
            ),
            step(say("Done.")),
        ]
    )
    kit = make_scope(store, pid, model, intent="edit", message="Change the words")
    result = await AgenticProfile().run(kit.scope, None)
    assert result == ProfileResult("Done.", "idle", True)
    text = kit.scope.staging.read("main.tex").text
    assert "Deep networks learn" in text and "One aggregate" in text
    answer = model.calls[1].messages[-1]
    assert answer.role == "user"
    assert [b.tool_use_id for b in answer.blocks if isinstance(b, ToolResultBlock)] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert [b.is_error for b in answer.blocks if isinstance(b, ToolResultBlock)] == [
        False,
        True,
        False,
    ]
    flow = [
        (type(e).__name__, getattr(e, "call_id", None))
        for e in kit.events
        if isinstance(e, ToolCallEvent | ToolResultEvent)
    ]
    assert flow == [
        ("ToolCallEvent", "call_1"),
        ("ToolResultEvent", "call_1"),
        ("ToolCallEvent", "call_2"),
        ("ToolResultEvent", "call_2"),
        ("ToolCallEvent", "call_3"),
        ("ToolResultEvent", "call_3"),
    ]
    started = [e for e in kit.events if isinstance(e, MessageStartedEvent)]
    calls = [e for e in kit.events if isinstance(e, ToolCallEvent)]
    assert {c.message_id for c in calls} == {started[0].message_id}
    assert calls[0].args == {"path": "main.tex", "old_str": "Graph", "new_str": "Deep"}
    assert kit.primed  # an edit Turn starts its Baseline
    assert len(model.calls) == 2 and model.calls[0].tools == AGENTIC_TOOLS


@pytest.mark.anyio
async def test_text_json_tool_call_is_recovered_for_flagged_models(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    spec = replace(fake_spec, quirks=frozenset({RECOVER_TEXT_TOOL_CALLS}))
    written = (
        "Let me look.\n```json\n"
        '{"name": "view", "arguments": {"path": "main.tex", "start_line": null, '
        '"end_line": null}}\n```'
    )
    model = FakeModel([step(say(written)), step(say("It says Hello."))], spec=spec)
    kit = make_scope(store, pid, model, intent="question", message="What does it say?")
    result = await AgenticProfile().run(kit.scope, None)
    assert result == ProfileResult("It says Hello.", "idle", False)
    [called] = [e for e in kit.events if isinstance(e, ToolCallEvent)]
    assert (called.call_id, called.name, called.recovered) == ("rec_1", "view", True)
    [result_event] = [e for e in kit.events if isinstance(e, ToolResultEvent)]
    assert result_event.ok
    assert [e.code for e in kit.events if isinstance(e, NoticeEvent)] == [
        "text_tool_call_recovered"
    ]
    assistant, answer = model.calls[1].messages[-2:]
    assert (
        ToolUseBlock("rec_1", "view", {"path": "main.tex", "start_line": None, "end_line": None})
        in assistant.blocks
    )
    [block] = answer.blocks
    assert isinstance(block, ToolResultBlock) and block.tool_use_id == "rec_1"
    assert "main.tex · lines 1-4 of 4" in block.content


@pytest.mark.anyio
async def test_unreadable_text_tool_call_gets_one_reprompt_per_turn(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    spec = replace(fake_spec, quirks=frozenset({RECOVER_TEXT_TOOL_CALLS}))
    broken = '{"name": "view", "arguments": {"path": }}'
    model = FakeModel([step(say(broken)), step(say(broken))], spec=spec)
    kit = make_scope(store, pid, model, intent="edit", message="Fix it")
    result = await AgenticProfile().run(kit.scope, None)
    assert result.stop == "idle" and len(model.calls) == 2
    reprompt = first_text(model.calls[1].messages[-1])
    assert reprompt.startswith("Your tool call was written as text and could not be read: ")
    assert reprompt.endswith("Call the tool with the tool-calling format.")


@pytest.mark.anyio
async def test_question_turn_keeps_the_full_tool_array_for_transcript_models_but_refuses_writes(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    edit_model = FakeModel([step(say("ok"))], spec=anthropic_like)
    await AgenticProfile().run(
        make_scope(store, pid, edit_model, intent="edit", message="Fix it").scope, None
    )
    model = FakeModel(
        [step(tool("str_replace", FIX)), step(say("A greeting."))], spec=anthropic_like
    )
    kit = make_scope(store, pid, model, intent="question", message="What is this?")
    result = await AgenticProfile().run(kit.scope, None)
    assert result == ProfileResult("A greeting.", "idle", False)
    assert model.calls[0].tools == edit_model.calls[0].tools == AGENTIC_TOOLS
    assert model.calls[0].system == edit_model.calls[0].system
    [refused] = [e for e in kit.events if isinstance(e, ToolResultEvent)]
    assert refused.error_code == "read_only_turn" and not kit.scope.staging.changed_since(0)
    opening = first_text(model.calls[0].messages[0])
    assert QUESTION_NOTE in opening and "<excerpt>" in opening and "  3| Hello" in opening
    assert QUESTION_NOTE not in first_text(edit_model.calls[0].messages[0])

    records = FakeModel([step(say("A greeting."))])
    await AgenticProfile().run(
        make_scope(store, pid, records, intent="question", message="What is this?").scope, None
    )
    assert records.calls[0].tools == READ_ONLY_TOOLS


@pytest.mark.anyio
async def test_max_tokens_mid_tool_call_gets_an_error_tool_result(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    model = FakeModel(
        [
            step(
                say("Editing."),
                tool("str_replace", '{"path": "main.tex", "old_str": "He'),
                truncate(),
            ),
            step(say("The edit was too long; I stopped.")),
        ]
    )
    kit = make_scope(store, pid, model, intent="edit", message="Fix it")
    result = await AgenticProfile().run(kit.scope, None)
    assert result.stop == "idle"
    answer = model.calls[1].messages[-1]
    assert answer.blocks == (ToolResultBlock("call_1", TRUNCATED_ARGS, is_error=True),)
    [cut] = [e for e in kit.events if isinstance(e, ToolResultEvent)]
    assert cut.error_code == "truncated_args"


@pytest.mark.anyio
async def test_refusal_ends_the_run_refused_with_its_category(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    model = FakeModel([refuse("cyber")])
    profile = AgenticProfile()
    result = await profile.run(make_scope(store, pid, model, intent="edit").scope, None)
    assert result.stop == "refused" and profile.refusal_category == "cyber"
    assert len(model.calls) == 1


@pytest.mark.anyio
async def test_repeat_guard_gives_up_and_sends_the_stop_hint_after_the_results(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    miss = '{"path": "main.tex", "old_str": "Goodbye", "new_str": "Hi"}'
    model = FakeModel([step(tool("str_replace", miss))], loop=True)
    kit = make_scope(store, pid, model, intent="edit", message="Fix it")
    result = await AgenticProfile().run(kit.scope, None)
    assert result.stop == "gave_up" and len(model.calls) == 3
    second = model.calls[2].messages[-1]
    [results, hint] = second.blocks
    assert isinstance(results, ToolResultBlock) and not results.content.endswith(STOP_HINT)
    assert hint == TextBlock(STOP_HINT)
    first = model.calls[1].messages[-1]
    assert len(first.blocks) == 1  # no hint after the first failure


@pytest.mark.anyio
async def test_repair_round_appends_one_message_with_the_new_errors_to_the_conversation(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    bold = '{"path": "main.tex", "old_str": "Hello", "new_str": "Hello \\\\textbff{x}"}'
    model = FakeModel([step(tool("str_replace", bold)), step(say("Done.")), step(say("Fixed."))])
    kit = make_scope(store, pid, model, intent="edit", message="Bold the greeting")
    profile = AgenticProfile()
    assert (await profile.run(kit.scope, None)).reply == "Done."
    problem = DiagnosticView(
        "main.tex", 3, "error", "undefined_control_sequence", "Undefined control sequence", None
    )
    feedback = RepairFeedback(1, (problem,), ("added \\usepackage{booktabs}",))
    result = await profile.run(kit.scope, feedback)
    assert result == ProfileResult("Fixed.", "idle", True)
    repair = model.calls[2].messages
    assert repair[: len(model.calls[1].messages)] == model.calls[1].messages
    assert len(repair) == len(model.calls[1].messages) + 2  # the closing reply, then the repair
    assert first_text(repair[-1]) == (
        "Your changes introduced 1 new compile error (repair round 1 of 2):\n"
        "main.tex:3: Undefined control sequence\n"
        "   2| \\begin{document}\n"
        " > 3| Hello \\textbff{x}\n"
        "   4| \\end{document}\n"
        "The host already added \\usepackage{booktabs}. Fix the error with the edit tools and "
        "change nothing else."
    )
    assert repair_message(feedback, kit.scope.staging) == first_text(repair[-1])
    assert model.calls[2].tools == model.calls[0].tools
    assert [m.role for m in profile.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


@pytest.mark.anyio
async def test_first_message_packs_the_volatile_block_with_the_request_last(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put, PAPER)
    version = store.read(pid, "main.tex").version
    editor = EditorContext(
        "main.tex", version, cursor_line=4, selection=Selection(4, 1, 5, 1), buffer=None
    )
    problem = DiagnosticView("main.tex", 6, "warning", "overfull", "Overfull \\hbox", None)
    model = FakeModel([step(say("ok"))])
    kit = make_scope(
        store,
        pid,
        model,
        intent="edit",
        editor=editor,
        message="Tighten this",
        diagnostics=(problem,),
    )
    await AgenticProfile().run(kit.scope, None)
    text = first_text(model.calls[0].messages[0])
    order = [text.index(tag) for tag in ("<project>", "<outline", "<diagnostics>", "<editor>")]
    assert order == sorted(order)
    assert text.endswith("<request>\nTighten this\n</request>")
    assert "Root file: main.tex. Engine: pdflatex." in text
    assert "main.tex:L3-L4 \\section{Introduction} \\label{sec:intro}" in text
    assert "Selection: lines 4-4" in text and " > 4| Graph networks" in text
    assert "   5| \\section{Method}" in text
    assert "<excerpt>" not in text and QUESTION_NOTE not in text


@pytest.mark.anyio
async def test_question_forced_read_follows_the_cursor_section_then_a_named_heading(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put, PAPER)
    version = store.read(pid, "main.tex").version
    model = FakeModel([step(say("ok"))], loop=True)
    at_cursor = make_scope(
        store,
        pid,
        model,
        intent="question",
        message="What does this say?",
        editor=EditorContext("main.tex", version, cursor_line=6),
    )
    await AgenticProfile().run(at_cursor.scope, None)
    excerpt = first_text(model.calls[0].messages[0]).split("<excerpt>\n")[1]
    assert excerpt.startswith("main.tex · lines 5-6 of 7")
    named = make_scope(store, pid, model, intent="question", message="Summarize the introduction")
    await AgenticProfile().run(named.scope, None)
    excerpt = first_text(model.calls[1].messages[0]).split("<excerpt>\n")[1]
    assert excerpt.startswith("main.tex · lines 3-4 of 7")
    whole = make_scope(store, pid, model, intent="question", message="Is it good?")
    await AgenticProfile().run(whole.scope, None)
    excerpt = first_text(model.calls[2].messages[0]).split("<excerpt>\n")[1]
    assert excerpt.startswith("main.tex · lines 1-7 of 7")


def _record(turn_id: str) -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        at="2026-11-28T00:00:00Z",
        status="completed",
        outcome="proposed",
        model=anthropic_like.ref,
        profile="agentic",
        message="Bold the greeting",
        summary="Proposed 1 change in main.tex. Compiles with no new errors.",
        proposal_id="p_1",
        targets=(),
        added_text=None,
    )


@pytest.mark.anyio
async def test_transcript_memory_replays_a_matching_segment_and_opens_a_new_one_otherwise(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    version = PromptSet.load().version("agentic", AGENTIC_TOOLS)
    key = SegmentKey.of(anthropic_like, "agentic", version)
    earlier = (
        Message("user", (TextBlock("<request>\nBold it\n</request>"),)),
        Message("assistant", (TextBlock("Bolded."),), {"role": "assistant", "content": []}),
    )
    segment = Segment(
        "s1", key.model, "agentic", key.prompt_version, key.effort, "new_thread", None
    )
    memory = ThreadMemory("th_1", (_record("t_0"),), segment, earlier, ())

    model = FakeModel([step(say("ok"))], spec=anthropic_like)
    profile = AgenticProfile()
    kit = make_scope(store, pid, model, intent="edit", memory=memory, message="Shorter")
    await profile.run(kit.scope, None)
    assert model.calls[0].messages[:2] == earlier and profile.segment is None
    assert "<earlier_turns>" not in first_text(model.calls[0].messages[2])
    assert [m.role for m in profile.messages] == ["user", "assistant"]

    stale = replace(memory, segment=replace(segment, prompt_version="00000000"))
    model = FakeModel([step(say("ok"))], spec=anthropic_like)
    profile = AgenticProfile()
    await profile.run(make_scope(store, pid, model, intent="edit", memory=stale).scope, None)
    assert profile.segment is not None and profile.segment.opened_by == "prompt_version"
    [opening] = model.calls[0].messages[:1]
    assert len(model.calls[0].messages) == 1
    assert first_text(opening).startswith("<earlier_turns>\nSummary of the earlier turns")


@pytest.mark.anyio
async def test_unknown_intent_counts_as_an_edit_only_once_the_model_tries_to_write(
    blank_project: Project, put: Put, make_scope: MakeScope
) -> None:
    store, pid = setup(blank_project, put)
    quiet = FakeModel([step(say("Hello to you too."))])
    kit = make_scope(store, pid, quiet, intent="unknown", message="Hello there")
    assert await AgenticProfile().run(kit.scope, None) == ProfileResult(
        "Hello to you too.", "idle", False
    )
    assert kit.primed == []
    busy = FakeModel([step(tool("str_replace", FIX)), step(say("Changed."))])
    kit = make_scope(store, pid, busy, intent="unknown", message="The greeting")
    result = await AgenticProfile().run(kit.scope, None)
    assert result == ProfileResult("Changed.", "idle", True) and kit.primed
