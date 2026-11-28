"""The Trace of a Turn: sequence, fan-out, replay from disk and the manifest (agent design §2.2)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from prismtex.agent import events as ev
from prismtex.agent.errors import TurnNotFound
from prismtex.agent.trace import TraceWriter, open_trace, prune_traces
from prismtex.agent.types import ErrorInfo, TurnOutcome, Usage

MakeTrace = Callable[..., TraceWriter]


def _started() -> ev.TurnStartedEvent:
    return ev.TurnStartedEvent(
        thread_id="th_1",
        profile="guided",
        model="fake:m",
        intent="edit",
        limits={"max_steps": 8, "max_wall_s": 180.0, "max_cost_usd": None},
    )


def _done() -> ev.DoneEvent:
    return ev.DoneEvent(
        status="completed",
        outcome="answered",
        verdict="no_change",
        summary="Answered. Nothing was changed.",
        reply="ok",
        proposal_id=None,
        usage=Usage(),
        error_code=None,
    )


@pytest.mark.anyio
async def test_trace_replays_events_after_seq_and_ends_after_done(
    tmp_path: Path, trace: TraceWriter
) -> None:
    trace.emit(_started())
    trace.emit(ev.StatusEvent(state="working", detail=None))
    trace.emit(ev.NoticeEvent(code="region_split", message="Split into 2 regions."))
    live: list[ev.Envelope] = []

    async def follow() -> None:
        async for env in trace.reader().subscribe(after=1):
            live.append(env)

    task = asyncio.create_task(follow())
    await asyncio.sleep(0)
    trace.emit(_done())
    await asyncio.wait_for(task, 1.0)
    assert [(e.seq, e.type) for e in live] == [(2, "status"), (3, "notice"), (4, "done")]
    replay = [e async for e in open_trace(tmp_path, "p1", "t_1").subscribe(after=2)]
    assert [(e.seq, e.type) for e in replay] == [(3, "notice"), (4, "done")]


@pytest.mark.anyio
async def test_events_emitted_from_another_thread_reach_every_live_subscriber(
    trace: TraceWriter,
) -> None:
    trace.emit(_started())
    seen: dict[str, list[int]] = {"a": [], "b": []}

    async def follow(name: str) -> None:
        async for env in trace.reader().subscribe():
            seen[name].append(env.seq)

    tasks = [asyncio.create_task(follow(name)) for name in seen]
    await asyncio.sleep(0)

    def work() -> None:
        for _ in range(50):
            trace.emit(ev.TokenEvent(message_id="m1", delta="x"))
        trace.emit(_done())

    await asyncio.to_thread(work)
    await asyncio.wait_for(asyncio.gather(*tasks), 1.0)
    assert seen["a"] == seen["b"] == list(range(1, 53))


def _turns(data_dir: Path, project_id: str = "p1") -> Path:
    return data_dir / "state" / project_id / "turns"


def _model_call() -> ev.ModelCallRecord:
    return ev.ModelCallRecord(
        step=1,
        kind="reply",
        model="fake:m",
        prompt_version="test",
        estimated_input_tokens=1200,
        input_tokens=1180,
        output_tokens=40,
        cache_read_tokens=None,
        cache_write_tokens=None,
        latency_ms=310,
        stop="end",
        parts={"system": 800, "request": 380},
    )


def _outcome(turn_id: str = "t_1", **changes: Any) -> TurnOutcome:
    values: dict[str, Any] = {
        "turn_id": turn_id,
        "thread_id": "th_1",
        "status": "completed",
        "outcome": "answered",
        "verdict": "no_change",
        "summary": "Answered. Nothing was changed.",
        "reply": "ok",
        "proposal": None,
        "usage": Usage(),
        "error": None,
    }
    values.update(changes)
    return TurnOutcome(**values)


@dataclass(frozen=True)
class _Proposal:
    id: str


def test_emit_after_done_raises_and_done_is_written_last(
    tmp_path: Path, trace: TraceWriter
) -> None:
    trace.emit(_started())
    trace.emit(ev.StatusEvent(state="working"))
    trace.emit(_done())
    with pytest.raises(RuntimeError):
        trace.emit(ev.StatusEvent(state="finalizing"))
    with pytest.raises(RuntimeError):
        trace.emit(_model_call())
    raw = (_turns(tmp_path) / "t_1.jsonl").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["turn_started", "status", "done"]
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3]


@pytest.mark.anyio
async def test_internal_events_are_written_but_never_streamed(
    tmp_path: Path, trace: TraceWriter
) -> None:
    early = trace.emit(ev.DecisionRecord(kind="profile", choice="guided"))
    trace.emit(_started())
    record = trace.emit(_model_call())
    hidden = trace.emit(
        ev.ErrorEvent(code="internal", message="KeyError: 'x'", retryable=False, fatal=False),
        internal=True,
    )
    trace.emit(_done())
    # Internal envelopes take no seq of their own, so the public stream has no gaps.
    assert (early.seq, record.seq, hidden.seq) == (0, 1, 1)
    assert early.internal and record.internal and hidden.internal
    for reader in (trace.reader(), open_trace(tmp_path, "p1", "t_1")):
        assert [e.type for e in reader.envelopes(internal=True)] == [
            "decision",
            "turn_started",
            "model_call",
            "error",
            "done",
        ]
        assert [(e.seq, e.type) for e in reader.envelopes()] == [(1, "turn_started"), (2, "done")]
        assert [e.type async for e in reader.subscribe()] == ["turn_started", "done"]
    with pytest.raises(ValueError):
        record.to_sse()


def test_done_cannot_be_internal(trace: TraceWriter) -> None:
    with pytest.raises(ValueError):
        trace.emit(_done(), internal=True)
    assert trace.reader().envelopes(internal=True) == []


@pytest.mark.anyio
async def test_released_writer_readers_replay_from_the_file(trace: TraceWriter) -> None:
    trace.emit(_started())
    trace.emit(ev.StatusEvent(state="working", detail="Editing sections/método.tex L42-60"))
    trace.emit(ev.TokenEvent(message_id="m1", delta="I propose …\n"))
    trace.emit(_model_call())
    trace.emit(_done())
    reader = trace.reader()
    before = [e async for e in reader.subscribe(after=0)]
    assert [e.seq for e in before] == [1, 2, 3, 4]
    pending = reader.subscribe(after=0)
    first = await anext(pending)

    trace.release()
    assert [first] + [e async for e in pending] == before
    assert [e async for e in reader.subscribe(after=0)] == before
    assert [e async for e in trace.reader().subscribe(after=0)] == before
    assert reader.envelopes() == before
    assert len(reader.envelopes(internal=True)) == 5


def test_release_before_done_raises(trace: TraceWriter) -> None:
    trace.emit(_started())
    with pytest.raises(RuntimeError):
        trace.release()


def test_set_state_replaces_the_manifest_atomically(
    tmp_path: Path, trace: TraceWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    calls: list[str] = []

    def shared_on_windows(src: str, dst: Path) -> None:
        calls.append(src)
        if len(calls) <= 2:
            raise PermissionError(32, "The file is being used by another process")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", shared_on_windows)
    trace.set_state("working")
    assert len(calls) == 3
    assert open_trace(tmp_path, "p1", "t_1").manifest().state == "working"
    assert trace.reader().manifest().state == "working"
    assert sorted(p.name for p in _turns(tmp_path).iterdir()) == ["t_1.json", "t_1.jsonl"]


def test_set_state_gives_up_after_five_retries_and_leaves_no_temp_file(
    tmp_path: Path, trace: TraceWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def always_shared(src: str, dst: Path) -> None:
        calls.append(src)
        raise PermissionError(32, "The file is being used by another process")

    monkeypatch.setattr(os, "replace", always_shared)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        trace.set_state("working")
    monkeypatch.undo()
    assert len(calls) == 6
    assert open_trace(tmp_path, "p1", "t_1").manifest().state == "accepted"
    assert sorted(p.name for p in _turns(tmp_path).iterdir()) == ["t_1.json", "t_1.jsonl"]


def test_close_writes_the_outcome_into_the_manifest(tmp_path: Path, trace: TraceWriter) -> None:
    trace.emit(_started())
    outcome = _outcome(
        status="failed",
        outcome="proposed",
        verdict="unverified",
        summary="Proposed 1 change in main.tex. Not compile-checked: TeX was not found.",
        proposal=_Proposal(id="t_1"),
        usage=Usage(steps=2, input_tokens=900, output_tokens=120, cost_usd=0.0021),
        error=ErrorInfo(code="provider_timeout", message="The model timed out.", retryable=True),
    )
    with pytest.raises(RuntimeError):
        trace.close(outcome)
    done = trace.emit(_done())
    trace.close(outcome)
    with pytest.raises(ValueError):
        trace.close(_outcome(turn_id="t_other"))

    manifest = open_trace(tmp_path, "p1", "t_1").manifest()
    assert manifest == trace.reader().manifest()
    assert manifest.state == "done"
    assert manifest.finished_at == done.at
    assert (manifest.status, manifest.outcome, manifest.verdict) == (
        "failed",
        "proposed",
        "unverified",
    )
    assert manifest.summary == outcome.summary
    assert manifest.proposal_id == "t_1"
    assert manifest.usage == outcome.usage
    assert manifest.error == outcome.error
    assert (manifest.thread_id, manifest.model, manifest.profile) == ("th_1", "fake:m", "agentic")
    saved = json.loads((_turns(tmp_path) / "t_1.json").read_text(encoding="utf-8"))
    assert saved["v"] == 1


@pytest.mark.anyio
async def test_a_writer_over_an_interrupted_trace_continues_its_sequence(
    tmp_path: Path, make_trace: MakeTrace
) -> None:
    folder = _turns(tmp_path)
    folder.mkdir(parents=True)
    lines = [
        ev.Envelope.of(_started(), seq=1, turn_id="t_1", at="2026-11-25T00:00:00.000Z"),
        ev.Envelope.of(
            ev.StatusEvent(state="working"), seq=2, turn_id="t_1", at="2026-11-25T00:00:01.000Z"
        ),
    ]
    torn = b'{"v":1,"seq":3,"turn_id":"t_1","at":"2026-11-25T00:00:02.0'
    body = b"".join(e.to_json().encode() + b"\n" for e in lines)
    (folder / "t_1.jsonl").write_bytes(body + torn)
    (folder / "t_1.json").write_text("{}", encoding="utf-8")
    # A reader over the files of an unfinished Trace ends at the end of the file.
    assert [e async for e in open_trace(tmp_path, "p1", "t_1").subscribe()] == lines

    writer = make_trace()
    error = writer.emit(
        ev.ErrorEvent(code="interrupted", message="The app stopped.", retryable=False, fatal=True)
    )
    done = writer.emit(_done())
    assert (error.seq, done.seq) == (3, 4)
    text = (folder / "t_1.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["seq"] for line in text.splitlines()] == [1, 2, 3, 4]

    with pytest.raises(RuntimeError):
        make_trace().emit(ev.StatusEvent(state="finalizing"))


def test_prune_traces_keeps_the_newest_200(
    tmp_path: Path, make_trace: MakeTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _fd: None)
    for i in range(205):
        # Names run against start times, so only the start time can order them.
        turn_id = f"t_{204 - i:03d}"
        started_at = f"2026-11-25T{i // 60:02d}:{i % 60:02d}:00Z"
        writer = make_trace(turn_id=turn_id, started_at=started_at)
        writer.emit(_started())
        writer.emit(_done())
        writer.close(_outcome(turn_id))
    assert prune_traces(tmp_path, "p1") == 5
    names = {p.name for p in _turns(tmp_path).iterdir()}
    kept = range(200)
    assert names == {f"t_{n:03d}.json" for n in kept} | {f"t_{n:03d}.jsonl" for n in kept}
    assert prune_traces(tmp_path, "p1") == 0


def test_prune_traces_never_deletes_an_unfinished_trace(
    tmp_path: Path, make_trace: MakeTrace
) -> None:
    make_trace(turn_id="t_old", started_at="2026-11-25T00:00:00Z").emit(_started())
    for turn_id, started_at in (
        ("t_mid", "2026-11-25T00:01:00Z"),
        ("t_new", "2026-11-25T00:02:00Z"),
    ):
        writer = make_trace(turn_id=turn_id, started_at=started_at)
        writer.emit(_done())
        writer.close(_outcome(turn_id))
    assert prune_traces(tmp_path, "p1", keep=1) == 1
    assert sorted(p.stem for p in _turns(tmp_path).glob("*.json")) == ["t_new", "t_old"]
    assert prune_traces(tmp_path, "p_missing") == 0


def test_open_trace_of_an_unknown_turn_raises_turn_not_found(
    tmp_path: Path, trace: TraceWriter
) -> None:
    with pytest.raises(TurnNotFound) as caught:
        open_trace(tmp_path, "p1", "t_missing")
    assert caught.value.code == "turn_not_found"
    assert caught.value.detail == {"turn_id": "t_missing"}
    for turn_id in ("", "../t_1", "t_1/../t_1", "t 1", "t_1.json"):
        with pytest.raises(TurnNotFound):
            open_trace(tmp_path, "p1", turn_id)
    with pytest.raises(TurnNotFound):
        open_trace(tmp_path, "p2", "t_1")
    with pytest.raises(TurnNotFound):
        open_trace(tmp_path, "../p1", "t_1")
    assert open_trace(tmp_path, "p1", "t_1").manifest().turn_id == "t_1"


def test_writer_refuses_ids_that_are_not_single_path_segments(make_trace: MakeTrace) -> None:
    with pytest.raises(ValueError):
        make_trace(turn_id="../t_1")
    with pytest.raises(ValueError):
        make_trace(project_id="p1/x")
