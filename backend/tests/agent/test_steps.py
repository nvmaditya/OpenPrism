"""One model call of a Turn: fit, reserve, stream, settle, events (agent design §2.2 `steps`)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from prismtex.agent.budget import DEFAULT_LIMITS, BudgetExhausted, BudgetGuard, Ledger, Limits
from prismtex.agent.context import ContextAssembler, ContextOverflow, PromptSet, TokenEstimator
from prismtex.agent.events import Envelope
from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ModelSpec,
    ProviderError,
    ProviderNotice,
    StepUsage,
    Stopped,
    TextBlock,
    TextDelta,
    ThinkingStarted,
    UsageReport,
)
from prismtex.agent.providers.fake import FakeModel, anthropic_like, fake_spec, say, step, tool
from prismtex.agent.steps import StepRunner
from prismtex.agent.tools import AGENTIC_TOOLS
from prismtex.agent.trace import TraceWriter
from prismtex.cancel import Cancelled, CancelToken

MakeTrace = Callable[..., TraceWriter]


class Port:
    """A model port that streams given events, then optionally fails."""

    def __init__(
        self,
        events: Sequence[ModelEvent],
        *,
        spec: ModelSpec = fake_spec,
        count: int | None = None,
        fail: ProviderError | None = None,
        after_first: Callable[[], None] | None = None,
    ) -> None:
        self.spec = spec
        self.calls: list[ModelCall] = []
        self.counted: list[ModelCall] = []
        self.closed = False
        self._events = list(events)
        self._count = count
        self._fail = fail
        self._after_first = after_first

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        self.calls.append(call)
        try:
            for index, event in enumerate(self._events):
                yield event
                if index == 0 and self._after_first is not None:
                    self._after_first()
            if self._fail is not None:
                raise self._fail
        finally:
            self.closed = True

    async def count_tokens(self, call: ModelCall) -> int | None:
        self.counted.append(call)
        return self._count


def ok(text: str) -> list[ModelEvent]:
    return [
        TextDelta(text),
        UsageReport(StepUsage(input_tokens=10, output_tokens=2)),
        Stopped("end", Message("assistant", (TextBlock(text),))),
    ]


def user(text: str) -> Message:
    return Message("user", (TextBlock(text),))


class Kit:
    def __init__(
        self,
        model: Any,
        trace: TraceWriter,
        *,
        limits: Limits = DEFAULT_LIMITS["agentic"],
        ledger: Ledger | None = None,
        max_usd: float = 1.0,
    ) -> None:
        spec: ModelSpec = model.spec
        self.trace = trace
        self.cancel = CancelToken()
        self.context = ContextAssembler(PromptSet.load(), TokenEstimator(spec.chars_per_token))
        self.budget = BudgetGuard(
            limits, spec, ledger, max_usd_per_turn=max_usd, cost_stop_usd=None
        )
        self.runner = StepRunner(
            model, self.context, self.budget, trace, self.cancel, clock=lambda: 0.0
        )

    def envelopes(self) -> list[Envelope]:
        return self.trace.reader().envelopes(internal=True)

    def types(self) -> list[str]:
        return [env.type for env in self.envelopes()]


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.anyio
async def test_step_runner_orders_message_events_coalesces_tokens_and_emits_usage_after_completion(
    make_trace: MakeTrace,
) -> None:
    model = FakeModel([step(say("x" * 400)), step(say("y"), tool("view", "{}"))])
    kit = Kit(model, make_trace())
    system = kit.context.system("agentic", fake_spec)
    call = ModelCall(system, (user("hi"),), AGENTIC_TOOLS)
    result = await kit.runner.run(call, kind="reply")
    envelopes = kit.envelopes()
    assert kit.types() == [
        "message_started",
        "token",
        "token",  # the first delta goes out at once; the rest wait for the 30 ms frame
        "message_completed",
        "usage",
        "model_call",
    ]
    assert envelopes[0].data == {"message_id": "m1", "step": 1}
    assert "".join(e.data["delta"] for e in envelopes if e.type == "token") == "x" * 400
    assert envelopes[3].data == {"message_id": "m1", "text": "x" * 400, "stop": "end"}
    usage = envelopes[4].data
    assert usage["step"] == 1 and usage["totals"]["steps"] == 1
    assert usage["step_usage"]["input_tokens"] > 0 and usage["remaining"]["steps"] == 23
    record = envelopes[5].data
    assert record["kind"] == "reply" and record["stop"] == "end" and record["step"] == 1
    assert record["prompt_version"] == PromptSet.load().version("agentic", AGENTIC_TOOLS)
    assert set(record["parts"]) == {"system", "tools", "messages"}
    assert (result.text, result.stop, result.message_id, result.tool_calls) == (
        "x" * 400,
        "end",
        "m1",
        (),
    )
    second = await kit.runner.run(call, kind="reply")
    assert second.message_id == "m2" and second.stop == "tool_use"
    assert [c.name for c in second.tool_calls] == ["view"]
    assert result.message.role == "assistant" and result.message.blocks == (TextBlock("x" * 400),)


@pytest.mark.anyio
async def test_draft_step_feeds_its_sink_and_route_step_streams_nothing(
    make_trace: MakeTrace,
) -> None:
    model = FakeModel([step(say("<<<REPLACEMENT\nNew.\nREPLACEMENT>>>")), step(say("{}"))])
    kit = Kit(model, make_trace())
    seen: list[str] = []
    draft = await kit.runner.run(ModelCall("s", (user("a"),)), kind="draft", sink=seen.append)
    assert "".join(seen) == draft.text == "<<<REPLACEMENT\nNew.\nREPLACEMENT>>>"
    assert draft.message_id is None
    route = await kit.runner.run(ModelCall("s", (user("b"),)), kind="route")
    assert route.text == "{}"
    assert kit.types() == ["usage", "model_call", "usage", "model_call"]


@pytest.mark.anyio
async def test_thinking_sets_the_status_detail_and_provider_notices_become_notices(
    make_trace: MakeTrace,
) -> None:
    notice = ProviderNotice("history_reset", "The conversation history was reset.")
    model = Port([ThinkingStarted(), ThinkingStarted(), notice, *ok("Fine.")])
    trace = make_trace()
    trace.set_state("repairing")
    kit = Kit(model, trace)
    await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    envelopes = kit.envelopes()
    assert kit.types()[:3] == ["status", "notice", "message_started"]
    assert envelopes[0].data == {"state": "repairing", "detail": "Thinking…"}
    assert envelopes[1].data == {"code": "history_reset", "message": notice.message}


@pytest.mark.anyio
async def test_aborted_stream_settles_at_the_reservation_and_still_records_the_call(
    make_trace: MakeTrace, tmp_path: Path
) -> None:
    path = tmp_path / "ledger.jsonl"
    failure = ProviderError("overloaded")
    model = Port([TextDelta("Par")], spec=anthropic_like, fail=failure)
    kit = Kit(model, make_trace(), ledger=Ledger(path))
    with pytest.raises(ProviderError):
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    assert model.closed
    assert kit.types() == ["message_started", "token", "model_call"]
    assert kit.envelopes()[-1].data["stop"] is None
    rows = ledger_rows(path)
    assert [row["kind"] for row in rows] == ["reserve", "settle"]
    assert rows[1]["estimated"] is True and rows[1]["micro_usd"] == rows[0]["micro_usd"]
    assert kit.budget.usage().estimated

    reported = Port(
        [UsageReport(StepUsage(input_tokens=40, output_tokens=0)), TextDelta("x")],
        spec=anthropic_like,
        fail=failure,
    )
    kit = Kit(reported, make_trace(turn_id="t_2"), ledger=Ledger(path))
    with pytest.raises(ProviderError):
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    settle = ledger_rows(path)[-1]
    assert settle["estimated"] is True and settle["usage"]["input_tokens"] == 40

    unfinished = Port([TextDelta("x")])
    kit = Kit(unfinished, make_trace(turn_id="t_3"))
    with pytest.raises(ProviderError) as caught:
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    assert caught.value.code == "bad_request"


@pytest.mark.anyio
async def test_context_overflow_is_raised_before_the_model_is_called(
    make_trace: MakeTrace,
) -> None:
    model = FakeModel([], spec=replace(fake_spec, context_window=2_048))
    kit = Kit(model, make_trace())
    with pytest.raises(ContextOverflow) as caught:
        await kit.runner.run(ModelCall("s", (user("x" * 10_000),)), kind="reply")
    assert model.calls == [] and caught.value.limit == 2_048 - 1_024 - 512


@pytest.mark.anyio
async def test_exact_count_decides_only_near_the_limit(make_trace: MakeTrace) -> None:
    spec = replace(fake_spec, context_window=4_096)  # input limit 4,096 - 1,024 - 512 = 2,560
    near = ModelCall("s", (user("x" * 6_500),))  # about 2,170 estimated tokens: within 20%
    far = ModelCall("s", (user("short"),))

    small = Port(ok("a"), spec=spec, count=1_000)
    kit = Kit(small, make_trace())
    await kit.runner.run(far, kind="reply")
    assert small.counted == []
    await kit.runner.run(near, kind="reply")
    assert len(small.counted) == 1 and len(small.calls) == 2

    large = Port(ok("a"), spec=spec, count=3_000)
    kit = Kit(large, make_trace(turn_id="t_2"))
    with pytest.raises(ContextOverflow) as caught:
        await kit.runner.run(near, kind="reply")
    assert caught.value.needed == 3_000 and large.calls == []


@pytest.mark.anyio
async def test_budget_near_fires_once_per_limit_at_80_percent(make_trace: MakeTrace) -> None:
    model = FakeModel([step(say("ok"))], loop=True)
    limits = Limits(max_steps=5, max_wall_s=600.0, max_tokens=800_000, max_tool_calls=60)
    kit = Kit(model, make_trace(), limits=limits)
    for _ in range(5):
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    notices = [e.data for e in kit.envelopes() if e.type == "notice"]
    assert notices == [
        {"code": "budget_near", "message": "This turn has used 4 of its 5 model calls."}
    ]
    with pytest.raises(BudgetExhausted) as caught:
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    assert caught.value.kind == "steps"


@pytest.mark.anyio
async def test_reserve_lowers_the_output_cap_to_fit_the_turn_cost_cap(
    make_trace: MakeTrace, tmp_path: Path
) -> None:
    model = FakeModel([step(say("ok"))], spec=anthropic_like)
    kit = Kit(model, make_trace(), ledger=Ledger(tmp_path / "ledger.jsonl"), max_usd=0.005)
    call = ModelCall("s", (user("a"),), max_output_tokens=16_000)
    await kit.runner.run(call, kind="reply")
    sent = model.calls[0].max_output_tokens
    assert 4_096 <= sent < 16_000


@pytest.mark.anyio
async def test_a_set_cancel_token_stops_before_the_call_and_mid_stream(
    make_trace: MakeTrace,
) -> None:
    model = FakeModel([step(say("never"))])
    kit = Kit(model, make_trace())
    kit.cancel.set()
    with pytest.raises(Cancelled):
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    assert model.calls == [] and kit.budget.usage().steps == 0

    trace = make_trace(turn_id="t_2")
    holder: list[CancelToken] = []
    port = Port([TextDelta("a"), TextDelta("b"), *ok("c")], after_first=lambda: holder[0].set())
    kit = Kit(port, trace)
    holder.append(kit.cancel)
    with pytest.raises(Cancelled):
        await kit.runner.run(ModelCall("s", (user("a"),)), kind="reply")
    assert port.closed and kit.budget.usage().estimated
    assert kit.types()[-1] == "model_call"
