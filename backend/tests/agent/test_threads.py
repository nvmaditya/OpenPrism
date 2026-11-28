"""Threads: the store and Thread memory (agent design §2.2 `threads`, §5.4, §10.3)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from prismtex.agent.errors import ThreadNotFound
from prismtex.agent.providers import Message, TextBlock, ToolResultBlock, ToolUseBlock
from prismtex.agent.threads import (
    DEFAULT_TITLE,
    RegionRef,
    Resolution,
    Segment,
    ThreadMemory,
    ThreadStore,
    TurnRecord,
    earlier_turns,
    render_records,
    render_since,
)

PID = "p1"
CLAUDE = "anthropic:claude-haiku-5-5"
QWEN = "ollama:qwen3:4b-instruct-2507-q4_K_M"


def record(turn_id: str, message: str = "Make it shorter.", **fields: Any) -> TurnRecord:
    base = TurnRecord(
        turn_id=turn_id,
        at=f"2026-11-28T10:00:{int(turn_id[1:]) % 60:02d}.000Z",
        status="completed",
        outcome="proposed",
        model=CLAUDE,
        profile="agentic",
        message=message,
        summary="Proposed 1 change in main.tex. Compiles with no new errors.",
        proposal_id=turn_id,
        targets=(RegionRef("main.tex", 9, 10),),
        added_text="We propose a simpler method.",
        reply="I shortened the sentence.",
        verdict="clean",
    )
    return replace(base, **fields)


def segment(segment_id: str = "s1", **fields: Any) -> Segment:
    base = Segment(segment_id, CLAUDE, "agentic", "1a2b3c4d", "medium", "new_thread", None)
    return replace(base, **fields)


def exchange(n: int) -> tuple[Message, ...]:
    """A user message, a tool call with opaque native thinking, its result and the answer."""
    thinking = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": f"sig{n}" * 8},
            {"type": "tool_use", "id": f"c{n}", "name": "view", "input": {"path": "main.tex"}},
        ],
    }
    return (
        Message("user", (TextBlock(f"<request>\nTurn {n} \\textbf{{x}} é\n</request>"),)),
        Message("assistant", (ToolUseBlock(f"c{n}", "view", {"path": "main.tex"}),), thinking),
        Message("user", (ToolResultBlock(f"c{n}", "  1| \\section{A}", is_error=False),)),
        Message("assistant", (TextBlock("Done."),), {"role": "assistant", "content": "Done."}),
    )


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_thread_store_appends_typed_v1_records_and_load_rebuilds_memory(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path)
    info = store.create(PID)
    tid = info.thread_id
    path = tmp_path / "state" / PID / "threads" / f"{tid}.jsonl"
    assert path.is_file() and store.exists(PID, tid)
    assert (info.title, info.turns, info.last_summary) == (DEFAULT_TITLE, 0, None)
    assert info.created_at == info.updated_at

    empty = store.load(PID, tid)
    assert empty == ThreadMemory(tid, (), None, (), (), ())

    first = record("t1")
    store.append_turn(PID, tid, first, exchange(1), segment())
    resolution = Resolution("t1", "2026-11-28T10:05:00.000Z", ("h1",), ("h2",), ("h3",))
    store.append_resolution(PID, tid, resolution)

    memory = store.load(PID, tid)
    assert memory.records == (first,)
    assert memory.segment == segment()
    assert memory.transcript == exchange(1)  # native payloads verbatim
    assert memory.since_last_turn == (resolution,)
    assert memory.resolutions == (resolution,)

    second = record("t2", proposal_id=None, outcome="answered", verdict="no_change")
    store.append_turn(PID, tid, second, exchange(2), None)  # continues the segment
    memory = store.load(PID, tid)
    assert memory.records == (first, second)
    assert memory.transcript == exchange(1) + exchange(2)
    assert memory.since_last_turn == ()
    assert memory.resolutions == (resolution,)

    # A rollover opens a new segment: only its own messages are replayed.
    rolled = segment("s2", opened_by="rollover", summary="<earlier_turns>\n...\n</earlier_turns>")
    store.append_turn(PID, tid, record("t3"), exchange(3), rolled)
    memory = store.load(PID, tid)
    assert memory.segment == rolled and memory.transcript == exchange(3)

    on_disk = lines(path)
    assert all(item["v"] == 1 for item in on_disk)
    assert [item["type"] for item in on_disk] == [
        "thread",
        "segment",
        "messages",
        "turn",
        "resolution",
        "messages",
        "turn",
        "segment",
        "messages",
        "turn",
    ]
    assert on_disk[0] == {
        "v": 1,
        "type": "thread",
        "thread_id": tid,
        "created_at": info.created_at,
        "title": None,
    }
    assert on_disk[2]["turn_id"] == "t1" and on_disk[1]["turn_id"] == "t1"
    assert on_disk[3]["targets"] == [{"path": "main.tex", "start_line": 9, "end_line": 10}]
    assert "é" in path.read_text(encoding="utf-8")  # UTF-8, not \\u escapes


def test_records_memory_keeps_no_transcript_and_turn_records_are_cut(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path)
    tid = store.create(PID).thread_id
    long = record("t1", "x" * 900, model=QWEN, profile="guided", added_text="y" * 3_000)
    assert len(long.message) == 500 and long.added_text == "y" * 1_000
    store.append_turn(PID, tid, long, (), None)
    memory = store.load(PID, tid)
    assert memory.records == (long,) and memory.segment is None and memory.transcript == ()
    with pytest.raises(ValueError, match="segment"):
        store.append_turn(PID, tid, record("t2", model=QWEN), exchange(2), None)
    assert len(store.load(PID, tid).records) == 1


def test_a_turn_on_another_model_ends_the_segment(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path)
    tid = store.create(PID).thread_id
    store.append_turn(PID, tid, record("t1"), exchange(1), segment())
    store.append_turn(PID, tid, record("t2", model=QWEN, profile="guided"), (), None)
    memory = store.load(PID, tid)
    # Replaying the Claude segment now would skip the qwen Turn, so there is none.
    assert memory.segment is None and memory.transcript == ()
    assert [r.turn_id for r in memory.records] == ["t1", "t2"]


def test_a_torn_last_line_is_skipped_and_the_next_append_starts_a_new_line(
    tmp_path: Path,
) -> None:
    store = ThreadStore(tmp_path)
    tid = store.create(PID).thread_id
    path = tmp_path / "state" / PID / "threads" / f"{tid}.jsonl"
    store.append_turn(PID, tid, record("t1"), exchange(1), segment())
    good = path.read_bytes()
    with path.open("ab") as out:
        out.write(b'{"v":1,"type":"turn","turn_id":"t2","at":"2026-')  # a crash mid-write
    memory = store.load(PID, tid)
    assert [r.turn_id for r in memory.records] == ["t1"]
    assert memory.transcript == exchange(1)

    store.append_turn(PID, tid, record("t3"), exchange(3), None)
    data = path.read_bytes()
    assert data.startswith(good) and data.endswith(b"\n")
    memory = store.load(PID, tid)
    assert [r.turn_id for r in memory.records] == ["t1", "t3"]
    assert memory.transcript == exchange(1) + exchange(3)

    # A Turn cut off before its turn record commits nothing, even its complete lines.
    orphan = {"v": 1, "turn_id": "t4", "segment_id": "s9", "model": CLAUDE, "profile": "agentic"}
    orphan |= {"prompt_version": "x", "effort": None, "opened_by": "rollover", "summary": None}
    with path.open("ab") as out:
        out.write(json.dumps({"type": "segment", **orphan}).encode() + b"\n")
        out.write(b'{"v":1,"type":"messages","turn_id":"t4","messages":[]}\n')
    store.append_turn(PID, tid, record("t5"), exchange(5), None)
    memory = store.load(PID, tid)
    assert memory.segment == segment()
    assert memory.transcript == exchange(1) + exchange(3) + exchange(5)
    # Lines of another version or of no known type are skipped too.
    with path.open("ab") as out:
        out.write(b'\n{"v":2,"type":"turn"}\n{"v":1,"type":"later"}\n[1,2]\n')
    assert [r.turn_id for r in store.load(PID, tid).records] == ["t1", "t3", "t5"]


def test_list_view_rename_and_delete(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path)
    named = store.create(PID, "  Methods   section ")
    assert named.title == "Methods section"
    untitled = store.create(PID)
    store.append_turn(
        PID,
        untitled.thread_id,
        record("t5", "Shorten the abstract, please.\nIt is long."),
        exchange(5),
        segment(),
    )
    infos = store.list(PID)
    assert [i.thread_id for i in infos] == [untitled.thread_id, named.thread_id]
    newest = infos[0]
    assert newest.title == "Shorten the abstract, please. It is long."
    assert (newest.turns, newest.updated_at) == (1, "2026-11-28T10:00:05.000Z")
    assert newest.last_summary == "Proposed 1 change in main.tex. Compiles with no new errors."
    assert newest.to_dict() == {
        "thread_id": untitled.thread_id,
        "title": newest.title,
        "created_at": untitled.created_at,
        "updated_at": "2026-11-28T10:00:05.000Z",
        "turns": 1,
        "last_summary": newest.last_summary,
    }

    view = store.view(PID, untitled.thread_id)
    assert view.to_dict()["turns"] == [
        {
            "turn_id": "t5",
            "at": "2026-11-28T10:00:05.000Z",
            "message": "Shorten the abstract, please.\nIt is long.",
            "reply": "I shortened the sentence.",
            "summary": "Proposed 1 change in main.tex. Compiles with no new errors.",
            "status": "completed",
            "outcome": "proposed",
            "verdict": "clean",
            "proposal_id": "t5",
            "model": CLAUDE,
            "profile": "agentic",
        }
    ]

    renamed = store.rename(PID, untitled.thread_id, "Abstract")
    assert renamed.title == "Abstract" and renamed.turns == 1
    assert (
        lines(tmp_path / "state" / PID / "threads" / f"{untitled.thread_id}.jsonl")[-1]["type"]
        == "title"
    )
    assert store.view(PID, untitled.thread_id).title == "Abstract"
    for bad in ("", "   ", "x" * 121):
        with pytest.raises(ValueError):
            store.rename(PID, untitled.thread_id, bad)

    store.delete(PID, untitled.thread_id)
    assert not store.exists(PID, untitled.thread_id)
    assert [i.thread_id for i in store.list(PID)] == [named.thread_id]
    with pytest.raises(ThreadNotFound):
        store.load(PID, untitled.thread_id)
    with pytest.raises(ThreadNotFound):
        store.delete(PID, untitled.thread_id)
    assert store.list("other") == []


def test_unknown_and_unsafe_thread_ids_are_not_found(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path)
    for tid in ("missing", "../x", "a/b", "C:\\x", ""):
        assert not store.exists(PID, tid)
        with pytest.raises(ThreadNotFound):
            store.load(PID, tid)
        with pytest.raises(ThreadNotFound):
            store.append_resolution(PID, tid, Resolution("t1", "2026-11-28T10:00:00Z", (), (), ()))
    assert not store.exists("../p", "x")
    with pytest.raises(ValueError):
        store.create("../p")
    # Appending never creates a Thread file.
    with pytest.raises(ThreadNotFound):
        store.append_turn(PID, "missing", record("t1"), (), None)
    assert not (tmp_path / "state" / PID / "threads" / "missing.jsonl").exists()


def test_records_memory_renders_the_last_3_or_6_records_and_earlier_turns_is_deterministic() -> (
    None
):
    records = tuple(
        record(f"t{n}", f"Request number {n}", proposal_id=f"t{n}" if n % 2 else None)
        for n in range(1, 15)
    )
    resolutions = (Resolution("t13", "2026-11-28T11:00:00.000Z", ("h1", "h2"), ("h3",), ()),)
    memory = ThreadMemory("th", records, None, (), resolutions, resolutions)

    guided = render_records(memory, profile="guided")
    assert guided.startswith("<memory>\n") and guided.endswith("\n</memory>")
    body = guided.split("\n")[1:-1]
    assert [line.split(":")[0] for line in body] == ["T-3", "T-2", "T-1"]
    assert body[0] == (
        'T-3: "Request number 12" -> Proposed 1 change in main.tex. Compiles with no new errors. '
        "Changed main.tex L9-L10."
    )
    assert body[1].endswith("Changed main.tex L9-L10. Review: 2 accepted, 1 rejected.")
    assert "Your reply" not in guided and "Text added" not in guided

    agentic = render_records(memory, profile="agentic")
    agentic_body = agentic.split("\n")[1:-1]
    assert [line.split(":")[0] for line in agentic_body[:6]] == [f"T-{n}" for n in range(6, 0, -1)]
    assert agentic_body[6:] == [
        'Text added in T-1: "We propose a simpler method."',
        'Your reply in T-1: "I shortened the sentence."',
    ]
    assert 'T-6: "Request number 9" ->' in agentic and "Replaced by a later proposal." in agentic
    assert render_records(memory, profile="agentic", limit=1).count("\nT-") == 1
    assert render_records(ThreadMemory("th", (), None, (), ()), profile="guided") == ""

    summary = earlier_turns(records, resolutions)
    assert summary == earlier_turns(records, resolutions)  # no clock, no randomness
    assert summary.startswith("<earlier_turns>\nSummary of the earlier turns")
    assert summary.count("\nT-") == 12 and 'T-12: "Request number 3"' in summary
    assert "Request number 2" not in summary
    assert earlier_turns(()) == ""


def test_pending_insertions_and_several_files_render_plainly() -> None:
    targets = (
        RegionRef("main.tex", 9, 9),
        RegionRef("main.tex", 13, 12),
        RegionRef("refs.bib", 1, 8),
    )
    last = record("t1", "Add a\n  citation", targets=targets)
    text = render_records(ThreadMemory("th", (last,), None, (), ()), profile="guided")
    assert text.split("\n")[1] == (
        'T-1: "Add a citation" -> Proposed 1 change in main.tex. Compiles with no new errors. '
        "Changed main.tex L9, after L12; refs.bib L1-L8. Pending review."
    )


def test_since_last_turn_reports_decisions_edits_and_a_stopped_turn() -> None:
    stopped = record("t1", status="cancelled", outcome="nothing")
    decided = (
        Resolution("t0", "2026-11-28T10:01:00.000Z", ("h1",), ("h2", "h3"), ("h4",)),
        Resolution("t0", "2026-11-28T10:02:00.000Z", ("h5",), (), ()),
    )
    memory = ThreadMemory("th", (stopped,), None, (), decided, decided)
    assert render_since(memory, changed_files=["main.tex", "refs.bib"]).split("\n") == [
        "<since_last_turn>",
        "The author accepted 2 changes and rejected 2 changes of the proposal.",
        "1 change could not be applied because the text had changed; it is still open.",
        "The author edited main.tex, refs.bib.",
        "Your previous turn was stopped before it finished.",
        "</since_last_turn>",
    ]
    calm = ThreadMemory("th", (record("t1"),), None, (), ())
    assert render_since(calm) == ""
