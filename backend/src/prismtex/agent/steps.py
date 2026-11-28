"""One model call of a Turn, shared by both Profiles (agent design §2.2 `steps`).

`StepRunner.run` does, in order: count the Step (`budget.step()`); when the
estimate is within 20% of the input limit, ask the Provider for an exact count;
`context.fit`, which refuses a call that cannot fit before anything is sent;
reserve the Step's worst case, which may lower `max_output_tokens`; stream;
settle the reservation, in a `finally`, so an aborted stream is settled at its
reservation; emit `usage` after `message_completed`; record `model_call`.

A `reply` Step streams prose as `message_started`, `token` and
`message_completed`, with token frames coalesced to at most one per 30 ms; a
`draft` Step hands text to its sink (the guided parser); a `route` Step streams
nothing. `ThinkingStarted` sets the status detail to "Thinking…", a
`ProviderNotice` becomes a `notice`, and `budget_near` fires once per limit
when 80% of it is used. `run` raises `ContextOverflow`, `BudgetExhausted`,
`ProviderError` or `Cancelled`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast, get_args

from prismtex.agent.budget import BudgetGuard, Reservation
from prismtex.agent.context import PROFILE_PROMPTS, ContextAssembler
from prismtex.agent.events import (
    MessageCompletedEvent,
    MessageStartedEvent,
    ModelCallRecord,
    NoticeEvent,
    RemainingJSON,
    StatusEvent,
    StatusState,
    StepUsageJSON,
    TokenEvent,
    UsageEvent,
)
from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ProviderError,
    ProviderNotice,
    StepUsage,
    Stopped,
    TextDelta,
    ThinkingStarted,
    ToolCall,
    ToolCallCompleted,
    UsageReport,
)
from prismtex.agent.trace import TraceWriter
from prismtex.cancel import CancelToken

__all__ = ["COALESCE_S", "NEAR_BUDGET", "StepKind", "StepResult", "StepRunner", "TextSink"]

TextSink = Callable[[str], None]
StepKind = Literal["reply", "draft", "route"]

COALESCE_S: Final = 0.030  # at most one token frame per 30 ms
NEAR_BUDGET: Final = 0.8  # budget_near fires once a limit is 80% used
THINKING: Final = "Thinking…"

_STATUS_STATES: Final = frozenset(get_args(StatusState))


@dataclass(frozen=True, slots=True)
class StepResult:
    """One finished Step.

    `message` is the assistant message exactly as received; `text` its prose;
    `tool_calls` the calls it made, in order. `usage` is what the Provider
    reported (zero output and the estimated input when it reported nothing).
    `message_id` names the message in `message_*` and `tool_call` events; it is
    None for `draft` and `route` Steps, which stream no message events.
    """

    message: Message
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop: Literal["end", "tool_use", "max_tokens", "refusal"]
    usage: StepUsage
    refusal_category: str | None
    message_id: str | None = None


class _Tokens:
    """Token frames of one message, coalesced to at most one per `COALESCE_S`."""

    def __init__(self, emit: Callable[[str], None], clock: Callable[[], float]) -> None:
        self._emit = emit
        self._clock = clock
        self._buffer: list[str] = []
        self._last: float | None = None
        self._timer: asyncio.TimerHandle | None = None

    def add(self, delta: str) -> None:
        if not delta:
            return
        self._buffer.append(delta)
        if self._timer is not None:
            return
        now = self._clock()
        wait = 0.0 if self._last is None else self._last + COALESCE_S - now
        if wait <= 0:
            self.flush()
        else:
            self._timer = asyncio.get_running_loop().call_later(wait, self.flush)

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._buffer:
            text, self._buffer = "".join(self._buffer), []
            self._last = self._clock()
            self._emit(text)

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


@dataclass
class _Stream:
    """What one stream produced so far."""

    text: list[str]
    calls: list[ToolCallCompleted]
    usage: StepUsage | None = None
    stopped: Stopped | None = None
    message_id: str | None = None


class StepRunner:
    """Runs the model calls of one Turn: one instance per Turn, shared by its Profile.

    `clock` measures latency and token coalescing; tests may pass their own.
    """

    def __init__(
        self,
        model: ModelPort,
        context: ContextAssembler,
        budget: BudgetGuard,
        trace: TraceWriter,
        cancel: CancelToken,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._context = context
        self._budget = budget
        self._trace = trace
        self._cancel = cancel
        self._clock = clock
        self._messages = 0
        self._near: set[str] = set()

    @property
    def model(self) -> ModelPort:
        return self._model

    async def run(
        self, call: ModelCall, *, kind: StepKind, sink: TextSink | None = None
    ) -> StepResult:
        """Run one Step and return it; see the module docstring for the order."""
        self._cancel.raise_if_set()
        self._budget.step()
        step = self._budget.usage().steps
        spec = self._model.spec
        exact: int | None = None
        if self._context.near_limit(call, spec):
            exact = await self._model.count_tokens(call)
        call = self._context.fit(call, spec, exact=exact)
        estimate = exact if exact is not None else self._context.estimate(call)
        reservation = await self._budget.reserve(estimate, call.max_output_tokens)
        if reservation.max_output != call.max_output_tokens:
            call = replace(call, max_output_tokens=reservation.max_output)

        started = self._clock()
        state = _Stream(text=[], calls=[])
        tokens = _Tokens(lambda text: self._token(state, text), self._clock)
        settled = False
        try:
            await self._stream(call, kind, sink, state, tokens, step)
            stopped = state.stopped
            assert stopped is not None
            await self._budget.settle(reservation, state.usage)
            settled = True
            if state.usage is not None:
                self._context.observe(call, _prompt_tokens(state.usage))
            if kind == "reply":
                tokens.flush()
                message_id = self._start_message(state, step)
                self._trace.emit(
                    MessageCompletedEvent(
                        message_id=message_id, text="".join(state.text), stop=stopped.reason
                    )
                )
            self._emit_usage(step, state.usage)
            self._near_limits()
        finally:
            tokens.cancel()
            if not settled:
                await self._settle_aborted(reservation, state.usage)
            self._record(call, kind, step, estimate, state, started)

        usage = state.usage or StepUsage(input_tokens=estimate, output_tokens=0)
        return StepResult(
            message=stopped.message,
            text="".join(state.text),
            tool_calls=tuple(state.calls),
            stop=stopped.reason,
            usage=usage,
            refusal_category=stopped.refusal_category,
            message_id=state.message_id,
        )

    # -- streaming -------------------------------------------------------------------

    async def _stream(
        self,
        call: ModelCall,
        kind: StepKind,
        sink: TextSink | None,
        state: _Stream,
        tokens: _Tokens,
        step: int,
    ) -> None:
        stream = self._model.stream(call)
        thinking = False
        try:
            async for event in stream:
                self._cancel.raise_if_set()
                if isinstance(event, TextDelta):
                    state.text.append(event.text)
                    if kind == "reply":
                        self._start_message(state, step)
                        tokens.add(event.text)
                    elif kind == "draft" and sink is not None:
                        sink(event.text)
                elif isinstance(event, ToolCallCompleted):
                    if kind == "reply":
                        tokens.flush()
                        self._start_message(state, step)
                    state.calls.append(event)
                elif isinstance(event, UsageReport):
                    state.usage = event.usage
                elif isinstance(event, ThinkingStarted):
                    if not thinking:
                        thinking = True
                        self._trace.emit(StatusEvent(state=self._state(), detail=THINKING))
                elif isinstance(event, ProviderNotice):
                    self._trace.emit(NoticeEvent(code=event.code, message=event.message))
                elif isinstance(event, Stopped):
                    state.stopped = event
                    break
                else:
                    _unexpected(event)
        finally:
            await _aclose(stream)
        if state.stopped is None:
            self._cancel.raise_if_set()
            raise ProviderError("bad_request", "The provider ended the reply without finishing it.")

    def _start_message(self, state: _Stream, step: int) -> str:
        if state.message_id is None:
            self._messages += 1
            state.message_id = f"m{self._messages}"
            self._trace.emit(MessageStartedEvent(message_id=state.message_id, step=step))
        return state.message_id

    def _token(self, state: _Stream, text: str) -> None:
        assert state.message_id is not None
        self._trace.emit(TokenEvent(message_id=state.message_id, delta=text))

    def _state(self) -> StatusState:
        current = self._trace.reader().manifest().state
        return cast(StatusState, current) if current in _STATUS_STATES else "working"

    # -- accounting ------------------------------------------------------------------

    async def _settle_aborted(self, reservation: Reservation, usage: StepUsage | None) -> None:
        if usage is None:
            await self._budget.settle(reservation, None)
        else:
            await self._budget.settle(reservation, usage, estimated=True)

    def _emit_usage(self, step: int, usage: StepUsage | None) -> None:
        remaining = self._budget.remaining()
        step_usage: StepUsageJSON | None = None
        if usage is not None:
            uncached, output, cache_read, cache_write = _counts(usage)
            step_usage = {
                "input_tokens": uncached + cache_read + cache_write,
                "output_tokens": output,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "served_model": usage.served_model or self._model.spec.model,
            }
        left: RemainingJSON = {
            "steps": remaining.steps,
            "tokens": remaining.tokens,
            "cost_usd": remaining.cost_usd,
            "wall_s": remaining.wall_s,
        }
        self._trace.emit(
            UsageEvent(
                step=step, step_usage=step_usage, totals=self._budget.usage(), remaining=left
            )
        )

    def _near_limits(self) -> None:
        limits = self._budget.limits
        remaining = self._budget.remaining()
        used = self._budget.usage()
        shares: dict[str, tuple[float, str]] = {}
        if limits.max_steps > 0:
            done = limits.max_steps - remaining.steps
            shares["steps"] = (
                done / limits.max_steps,
                f"{done} of its {limits.max_steps} model calls",
            )
        if limits.max_tokens > 0:
            done = limits.max_tokens - remaining.tokens
            shares["tokens"] = (
                done / limits.max_tokens,
                f"{done:,} of its {limits.max_tokens:,} tokens",
            )
        if limits.max_tool_calls > 0:
            done = limits.max_tool_calls - remaining.tool_calls
            shares["tool_calls"] = (
                done / limits.max_tool_calls,
                f"{done} of its {limits.max_tool_calls} tool calls",
            )
        if limits.max_wall_s > 0:
            spent = limits.max_wall_s - remaining.wall_s
            shares["wall_time"] = (
                spent / limits.max_wall_s,
                f"{spent:.0f} s of its {limits.max_wall_s:.0f} s",
            )
        if remaining.cost_usd is not None:
            total = used.cost_usd + remaining.cost_usd
            if total > 0:
                shares["cost"] = (
                    used.cost_usd / total,
                    f"${used.cost_usd:.2f} of its ${total:.2f}",
                )
        for kind, (share, amount) in shares.items():
            if share >= NEAR_BUDGET and kind not in self._near:
                self._near.add(kind)
                self._trace.emit(
                    NoticeEvent(code="budget_near", message=f"This turn has used {amount}.")
                )

    def _record(
        self,
        call: ModelCall,
        kind: StepKind,
        step: int,
        estimate: int,
        state: _Stream,
        started: float,
    ) -> None:
        counts = _counts(state.usage) if state.usage is not None else None
        self._trace.emit(
            ModelCallRecord(
                step=step,
                kind=kind,
                model=self._model.spec.ref,
                prompt_version=self._prompt_version(call),
                estimated_input_tokens=estimate,
                input_tokens=None if counts is None else counts[0] + counts[2] + counts[3],
                output_tokens=None if counts is None else counts[1],
                cache_read_tokens=None if counts is None else counts[2],
                cache_write_tokens=None if counts is None else counts[3],
                latency_ms=round((self._clock() - started) * 1000),
                stop=state.stopped.reason if state.stopped is not None else None,
                parts=self._context.breakdown(call),
            )
        )

    def _prompt_version(self, call: ModelCall) -> str:
        """The prompt version of the Profile whose prompt is the call's system string."""
        prompts = self._context.prompts
        for profile, names in PROFILE_PROMPTS.items():
            if any(prompts.texts.get(name) == call.system for name in names):
                return prompts.version(profile, call.tools)
        return "unknown"


def _counts(usage: StepUsage) -> tuple[int, int, int, int]:
    """Uncached input, output, cache-read and cache-write tokens, summed over iterations."""
    if not usage.iterations:
        return (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        )
    parts = [_counts(part) for part in usage.iterations]
    return (
        sum(p[0] for p in parts),
        sum(p[1] for p in parts),
        sum(p[2] for p in parts),
        sum(p[3] for p in parts),
    )


def _prompt_tokens(usage: StepUsage) -> int:
    uncached, _, cache_read, cache_write = _counts(usage)
    return uncached + cache_read + cache_write


def _unexpected(event: Any) -> None:
    raise TypeError(f"unexpected model event: {type(event).__name__}")


async def _aclose(stream: AsyncIterator[ModelEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()
