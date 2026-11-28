"""The event union v1: wire format, encoding rules and the reviewed schema (agent design §9)."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from prismtex.agent import events as ev
from prismtex.agent.trace import TraceWriter
from prismtex.agent.types import StaleHunk, Usage

SNAPSHOT = Path(__file__).parent / "snapshots" / "events.schema.json"
# Read at import: the autouse environment fixture strips PRISMTEX_* before tests run.
UPDATE_SNAPSHOTS = os.environ.get("PRISMTEX_UPDATE_SNAPSHOTS") == "1"

PUBLIC_TYPES = [
    "turn_started",
    "status",
    "region",
    "message_started",
    "token",
    "message_completed",
    "rewrite_delta",
    "tool_call",
    "tool_result",
    "compile",
    "diagnostics",
    "proposal",
    "resolved",
    "usage",
    "notice",
    "error",
    "done",
]

DIAGNOSTIC: ev.DiagnosticJSON = {
    "file": "main.tex",
    "line": 42,
    "severity": "error",
    "kind": "undefined_control_sequence",
    "message": "Undefined control sequence \\foo",
    "excerpt": "We use \\foo here.",
}

UNREPORTED_USAGE = ev.UsageEvent(
    step=0,
    step_usage=None,
    totals=Usage(),
    remaining={"steps": 8, "tokens": 16384, "cost_usd": None, "wall_s": 180.0},
)

# One instance of every public type, with optional fields both set and unset.
SAMPLES: list[ev.PublicEvent] = [
    ev.TurnStartedEvent(
        thread_id="th_1",
        profile="agentic",
        model="anthropic:claude-haiku",
        intent="question",
        limits={"max_steps": 12, "max_wall_s": 300.0, "max_cost_usd": 1.0},
    ),
    ev.StatusEvent(state="repairing", detail="Repair round 1 of 2"),
    ev.StatusEvent(state="verifying"),
    ev.RegionEvent(
        index=0,
        path="main.tex",
        start_line=10,
        end_line=14,
        kind="paragraph",
        label="Introduction, paragraph 2",
    ),
    ev.MessageStartedEvent(message_id="m1", step=1),
    ev.TokenEvent(message_id="m1", delta="Here"),
    ev.MessageCompletedEvent(message_id="m1", text="Here is the change.", stop="tool_use"),
    ev.RewriteDeltaEvent(index=0, delta="A shorter sentence."),
    ev.ToolCallEvent(
        call_id="c1",
        message_id="m1",
        name="str_replace",
        args={"path": "main.tex", "old": "a", "new": "b", "count": 1},
        recovered=False,
    ),
    ev.ToolResultEvent(call_id="c1", ok=True, summary="Edited main.tex L12", changed=("main.tex",)),
    ev.ToolResultEvent(
        call_id="c2", ok=False, summary="Not found", error_code="not_found", changed=()
    ),
    ev.CompileEvent(stage="baseline", round=0, state="started"),
    ev.CompileEvent(
        stage="proposal",
        round=1,
        state="finished",
        ok=False,
        errors=1,
        warnings=3,
        duration_ms=1840,
    ),
    ev.DiagnosticsEvent(
        round=1,
        new=(DIAGNOSTIC,),
        fixed=({**DIAGNOSTIC, "file": None, "line": None, "excerpt": None},),
        persisting_errors=0,
        host_fixes=("Added \\usepackage{booktabs}.",),
    ),
    ev.ProposalEvent(proposal={"id": "t_1", "files": [], "truncated": False}),
    ev.ResolvedEvent(
        proposal_id="t_1",
        applied=("h0123456789ab",),
        stale=(StaleHunk(hunk_id="hba9876543210", path="refs.bib", reason="context_changed"),),
        files=({"path": "main.tex", "version": "a" * 64}, {"path": "old.tex", "version": None}),
        checkpoint_id="c_1",
        by="auto",
    ),
    ev.UsageEvent(
        step=1,
        step_usage={
            "input_tokens": 1200,
            "output_tokens": 80,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 0,
            "served_model": "claude-haiku",
        },
        totals=Usage(steps=1, input_tokens=1200, output_tokens=80, cost_usd=0.0004),
        remaining={"steps": 11, "tokens": 98720, "cost_usd": 0.9996, "wall_s": 297.5},
    ),
    UNREPORTED_USAGE,
    ev.NoticeEvent(code="budget_near", message="80% of the step limit is used."),
    ev.ErrorEvent(
        code="context_overflow",
        message="The request does not fit the model's context.",
        retryable=False,
        fatal=True,
        detail={"parts": {"system": 900, "files": 15000}},
    ),
    ev.ErrorEvent(code="provider_timeout", message="Timed out.", retryable=True, fatal=False),
    ev.DoneEvent(
        status="completed",
        outcome="proposed",
        verdict="clean",
        summary="Proposed 2 changes in main.tex. Compiles with no new errors.",
        reply="Done.",
        proposal_id="t_1",
        usage=Usage(steps=2),
    ),
    ev.DoneEvent(
        status="failed",
        outcome="nothing",
        verdict="no_change",
        summary="No change made.",
        reply="",
        usage=Usage(),
        error_code="provider_auth",
    ),
]


def _wire(event: ev.Event, seq: int = 1) -> dict[str, Any]:
    envelope = ev.Envelope.of(event, seq=seq, turn_id="t_1", at="2026-11-25T00:00:00.000Z")
    return json.loads(envelope.to_json())


def test_sse_frame_has_id_event_and_compact_data() -> None:
    envelope = ev.Envelope.of(
        ev.TokenEvent(message_id="m1", delta="I propose "),
        seq=7,
        turn_id="t_20261124T231004_9f3a",
        at="2026-11-24T23:10:04.120Z",
    )
    assert envelope.to_sse() == (
        "id: 7\nevent: token\n"
        'data: {"v":1,"seq":7,"turn_id":"t_20261124T231004_9f3a","at":"2026-11-24T23:10:04.120Z",'
        '"type":"token","message_id":"m1","delta":"I propose "}\n\n'
    )


def test_trace_envelopes_carry_rfc3339_times_and_stay_one_line(trace: TraceWriter) -> None:
    for _ in range(6):
        trace.emit(ev.StatusEvent(state="working"))
    envelope = trace.emit(ev.TokenEvent(message_id="m1", delta="Thinking…\nnext line"))
    frame = envelope.to_sse()
    assert frame.startswith("id: 7\nevent: token\ndata: {")
    assert frame.endswith("}\n\n") and frame.count("\n") == 4  # the newline is escaped
    assert "Thinking…" in frame  # not \u2026
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", envelope.at)
    assert ev.Envelope.from_json(envelope.to_json()) == envelope


def test_optional_fields_are_left_out_until_set() -> None:
    assert _wire(ev.StatusEvent(state="working")) == {
        "v": 1,
        "seq": 1,
        "turn_id": "t_1",
        "at": "2026-11-25T00:00:00.000Z",
        "type": "status",
        "state": "working",
    }
    assert _wire(ev.StatusEvent(state="working", detail="Thinking…"))["detail"] == "Thinking…"
    started = _wire(ev.CompileEvent(stage="baseline", round=0, state="started"))
    assert not {"ok", "errors", "warnings", "duration_ms"} & started.keys()
    # A field that is not optional is always present, even when null.
    usage = _wire(UNREPORTED_USAGE)
    assert usage["step_usage"] is None
    assert usage["remaining"]["cost_usd"] is None
    assert usage["totals"] == dataclasses.asdict(Usage())


def test_tool_call_args_are_cut_at_200_characters_at_any_depth() -> None:
    event = ev.ToolCallEvent(
        call_id="c1",
        message_id="m1",
        name="replace_lines",
        args={"text": "x" * 500, "edits": [{"new": "y" * 300}], "start": 3},
        recovered=True,
    )
    assert event.args == {"text": "x" * 200, "edits": [{"new": "y" * 200}], "start": 3}


def test_tool_io_record_keeps_at_most_4_kib_of_utf8() -> None:
    record = ev.ToolIORecord(
        call_id="c1", name="view", args="é" * 3000, result="ok", ok=True, error_code=None
    )
    assert len(record.args.encode("utf-8")) == 4096
    assert record.args == "é" * 2048
    assert record.result == "ok"


def test_envelopes_round_trip_through_their_json() -> None:
    for event in [*SAMPLES, ev.DecisionRecord(kind="intent", choice="edit", detail={"rule": 2})]:
        envelope = ev.Envelope.of(event, seq=3, turn_id="t_1", at="2026-11-25T00:00:00.000Z")
        assert ev.Envelope.from_json(envelope.to_json()) == envelope
    hidden = ev.Envelope.of(
        ev.NoticeEvent(code="region_split", message="x"), seq=1, turn_id="t_1", at="", internal=True
    )
    assert json.loads(hidden.to_json())["internal"] is True
    assert ev.Envelope.from_json(hidden.to_json()).internal
    for line in ("[]", '{"v":1}', "not json", '{"v":1,"seq":"1","turn_id":"t","at":"","type":"x"}'):
        with pytest.raises(ValueError):
            ev.Envelope.from_json(line)


def test_the_union_has_17_public_types_and_3_internal_records() -> None:
    assert [cls.type for cls in ev.PUBLIC_EVENTS] == PUBLIC_TYPES
    assert all(cls.__name__.endswith("Event") for cls in ev.PUBLIC_EVENTS)
    assert ev.INTERNAL_TYPES == {"model_call", "tool_io", "decision"}
    assert len(get_args(ev.Event)) == 20
    for cls in get_args(ev.Event):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not names & {*ev.ENVELOPE_KEYS, "internal"}, cls.__name__
    assert {type(sample).type for sample in SAMPLES} == set(PUBLIC_TYPES)


def test_event_json_schema_matches_the_reviewed_snapshot() -> None:
    schema = ev.event_schema()
    if UPDATE_SNAPSHOTS:
        SNAPSHOT.parent.mkdir(exist_ok=True)
        text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        SNAPSHOT.write_text(text, encoding="utf-8", newline="\n")
    assert SNAPSHOT.is_file(), "run with PRISMTEX_UPDATE_SNAPSHOTS=1 to write the snapshot"
    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == schema, (
        "the event schema changed; review it, then rerun with PRISMTEX_UPDATE_SNAPSHOTS=1"
    )


def test_schema_covers_the_public_types_only_at_v1() -> None:
    schema = ev.event_schema()
    refs = [entry["$ref"].removeprefix("#/$defs/") for entry in schema["oneOf"]]
    assert refs == [cls.__name__ for cls in ev.PUBLIC_EVENTS]
    for name in refs:
        properties = schema["$defs"][name]["properties"]
        assert properties["v"]["const"] == 1
    assert not {"ModelCallRecord", "ToolIORecord", "DecisionRecord"} & schema["$defs"].keys()


@pytest.mark.parametrize("event", SAMPLES, ids=lambda e: type(e).__name__)
def test_every_sample_event_validates_against_the_schema(event: ev.PublicEvent) -> None:
    schema = ev.event_schema()
    wire = _wire(event)
    _validate(wire, {"$ref": f"#/$defs/{type(event).__name__}"}, schema["$defs"])
    matches = [ref for ref in schema["oneOf"] if _valid(wire, ref, schema["$defs"])]
    assert len(matches) == 1


# A validator for the subset of JSON Schema that event_schema() emits.


def _valid(value: Any, schema: dict[str, Any], defs: dict[str, Any]) -> bool:
    try:
        _validate(value, schema, defs)
    except AssertionError:
        return False
    return True


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
    "object": (dict,),
    "array": (list,),
}


def _validate(value: Any, schema: dict[str, Any], defs: dict[str, Any]) -> None:
    if "$ref" in schema:
        _validate(value, defs[schema["$ref"].removeprefix("#/$defs/")], defs)
        return
    if "anyOf" in schema:
        assert any(_valid(value, option, defs) for option in schema["anyOf"]), value
        return
    if "type" in schema:
        wanted = _TYPES[schema["type"]]
        assert isinstance(value, wanted), (value, schema)
        if schema["type"] in ("integer", "number"):
            assert not isinstance(value, bool), value
    if "const" in schema:
        assert value == schema["const"], (value, schema)
    if "enum" in schema:
        assert value in schema["enum"], (value, schema)
    if "minimum" in schema:
        assert value >= schema["minimum"], (value, schema)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            assert name in value, (name, value)
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], defs)
            elif schema.get("additionalProperties") is False:
                raise AssertionError(f"unexpected field {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate(item, schema["additionalProperties"], defs)
    if isinstance(value, list) and "items" in schema:
        for item in value:
            _validate(item, schema["items"], defs)
