"""Fake, recording and replay adapters (agent design §11.5).

- `FakeModel(script)` plays scripted Steps: `step(say(...), tool(...))`, `fail(error)`,
  `refuse(category)`, `truncate()`, or a callable `(ModelCall) -> Step`. Text streams in
  4-character deltas and tool arguments pass through as the raw strings given, so
  single-escaped LaTeX can be tested. Every `ModelCall` is kept in `.calls`.
- `RecordingModel(inner, path)` appends each request and its events to a cassette;
  `ReplayModel(path, spec=...)` serves them back and fails with a diff on prompt drift.
- `FakeResolver`, `RecordingModels` and `ReplayModels` are the matching resolvers.
- `scripted_model(path)` builds the `fake:scripted` model from a JSON script file.

A cassette is JSONL, one line per request: `{"hash", "model", "request", "events"}`.
The hash is SHA-256 over the canonical JSON of the request: system, tools (name,
description, schema), messages (neutral blocks), max output tokens and response schema.
Lines without a "hash" are ignored, so a cassette may carry a header line.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import math
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, assert_never

from prismtex.agent.providers import (
    Block,
    HealthReport,
    Message,
    ModelCall,
    ModelEvent,
    ModelInfo,
    ModelPort,
    ModelResolver,
    ModelSpec,
    ModelUnavailable,
    ProviderError,
    ProviderHealth,
    ProviderNotice,
    StepUsage,
    Stopped,
    StopReason,
    TextBlock,
    TextDelta,
    ThinkingStarted,
    ToolCallCompleted,
    ToolResultBlock,
    ToolUseBlock,
    UsageReport,
    catalog,
)

__all__ = [
    "Cassette",
    "FakeModel",
    "FakeResolver",
    "Part",
    "RecordingModel",
    "RecordingModels",
    "ReplayMismatch",
    "ReplayModel",
    "ReplayModels",
    "Say",
    "ScriptExhausted",
    "Step",
    "ToolPart",
    "Truncate",
    "anthropic_like",
    "fail",
    "fake_spec",
    "refuse",
    "say",
    "scripted_model",
    "step",
    "tool",
    "truncate",
]

# "fake:m": agentic, records memory, native tools, window 32,768, max output 1,024, free.
fake_spec: ModelSpec = catalog.FAKE_SPEC
# Transcript memory, window 1,000,000, cap 100,000, max output 16,000, Haiku 5.5 prices,
# paid; its Steps carry opaque native payloads with thinking signatures.
anthropic_like: ModelSpec = catalog.ANTHROPIC_LIKE

_DELTA = 4  # characters per streamed text delta


# -- scripts ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Say:
    text: str


@dataclass(frozen=True, slots=True)
class ToolPart:
    name: str
    raw_json: str


@dataclass(frozen=True, slots=True)
class Truncate:
    pass


Part = Say | ToolPart | Truncate


@dataclass(frozen=True, slots=True)
class Step:
    """One scripted model call: parts in order, or an error, or a refusal."""

    parts: tuple[Part, ...] = ()
    error: ProviderError | None = None
    refusal: str | None = None
    usage: StepUsage | None = None
    delay_ms: int = 0


def say(text: str) -> Part:
    return Say(text)


def tool(name: str, raw_json: str) -> Part:
    """A tool call whose arguments are `raw_json` exactly, never parsed or repaired."""
    return ToolPart(name, raw_json)


def truncate() -> Part:
    """End the Step at `max_tokens`; a tool part just before it stays open."""
    return Truncate()


def step(*parts: Part, usage: StepUsage | None = None, delay_ms: int = 0) -> Step:
    """A Step streaming `parts`; `usage` replaces the estimated usage report."""
    for index, part in enumerate(parts):
        if isinstance(part, Truncate) and index != len(parts) - 1:
            raise ValueError("truncate() must be the last part of a step")
    if delay_ms < 0:
        raise ValueError("delay_ms must not be negative")
    return Step(parts=parts, usage=usage, delay_ms=delay_ms)


def fail(error: ProviderError) -> Step:
    """A Step that raises `error` before any event."""
    return Step(error=error)


def refuse(category: str) -> Step:
    """A Step that stops with `refusal` and the given category."""
    return Step(refusal=category)


class ScriptExhausted(Exception):
    """A FakeModel was called more often than its script has Steps."""


Script = Sequence[Step | Callable[[ModelCall], Step]]


class FakeModel(ModelPort):
    """A scripted model. With `loop`, the script restarts when it runs out."""

    def __init__(self, script: Script, *, spec: ModelSpec = fake_spec, loop: bool = False) -> None:
        self.spec = spec
        self.calls: list[ModelCall] = []
        self._script = tuple(script)
        self._next = 0
        self._loop = loop
        self._tool_ids = 0

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        self.calls.append(call)
        planned = self._take(call)
        if planned.delay_ms:
            await asyncio.sleep(planned.delay_ms / 1000)
        if planned.error is not None:
            raise planned.error
        transcript = self.spec.memory == "transcript"
        if transcript:
            yield ThinkingStarted()
        blocks: list[Block] = []
        written = 0
        reason: StopReason = "end"
        for index, part in enumerate(planned.parts):
            if isinstance(part, Say):
                if not part.text:
                    continue
                for offset in range(0, len(part.text), _DELTA):
                    yield TextDelta(part.text[offset : offset + _DELTA])
                written += len(part.text)
                if blocks and isinstance(blocks[-1], TextBlock):
                    blocks[-1] = TextBlock(blocks[-1].text + part.text)
                else:
                    blocks.append(TextBlock(part.text))
            elif isinstance(part, ToolPart):
                left_open = isinstance(_at(planned.parts, index + 1), Truncate)
                args = None if left_open else _object_or_none(part.raw_json)
                self._tool_ids += 1
                call_id = f"call_{self._tool_ids}"
                yield ToolCallCompleted(
                    id=call_id,
                    name=part.name,
                    raw_args=part.raw_json,
                    args=args,
                    parse_error="truncated" if left_open else None,
                    recovered=False,
                )
                written += len(part.raw_json)
                blocks.append(ToolUseBlock(call_id, part.name, args or {}))
                reason = "tool_use"
            else:
                reason = "max_tokens"
        if planned.refusal is not None:
            reason = "refusal"
        yield UsageReport(planned.usage or self._usage(call, written))
        native = self._native(blocks, len(self.calls)) if transcript else None
        yield Stopped(reason, Message("assistant", tuple(blocks), native), planned.refusal)

    async def count_tokens(self, call: ModelCall) -> int | None:
        return None

    def assert_consumed(self) -> None:
        """Fail if any scripted Step was never used."""
        left = len(self._script) - self._next
        if left:
            raise AssertionError(f"{left} of {len(self._script)} scripted steps were never used")

    def _take(self, call: ModelCall) -> Step:
        if self._next == len(self._script) and self._loop and self._script:
            self._next = 0
        if self._next == len(self._script):
            raise ScriptExhausted(
                f"the script has {len(self._script)} steps; call {len(self.calls)} wants another"
            )
        entry = self._script[self._next]
        self._next += 1
        return entry if isinstance(entry, Step) else entry(call)

    def _usage(self, call: ModelCall, written: int) -> StepUsage:
        per_token = self.spec.chars_per_token
        return StepUsage(
            input_tokens=math.ceil(_call_chars(call) / per_token),
            output_tokens=math.ceil(written / per_token),
            served_model=self.spec.model,
        )

    def _native(self, blocks: Sequence[Block], number: int) -> dict[str, Any]:
        """An opaque assistant payload in the shape Anthropic returns, thinking first."""
        seed = f"{self.spec.ref}:{number}:{_canonical([_block_json(b) for b in blocks])}"
        signature = hashlib.sha256(seed.encode()).hexdigest()[:32]
        content: list[dict[str, Any]] = [
            {"type": "thinking", "thinking": "", "signature": signature}
        ]
        content.extend(_block_json(block) for block in blocks)
        return {"role": "assistant", "content": content}


def scripted_model(path: Path) -> FakeModel:
    """The `fake:scripted` model from a JSON script file.

    The file is `{"loop": true, "steps": [{"say": "...", "tools": [{"name": "...",
    "args": "<raw json>"}], "delay_ms": 0}]}`; each step's text streams before its tool
    calls. With `loop` the script restarts when it runs out, so one file serves any
    number of Turns.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the fake script {path.name}: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError(f"the fake script {path.name} needs a 'steps' list")
    _only_keys(data, {"loop", "steps"}, path.name)
    steps: list[Step] = []
    for number, entry in enumerate(data["steps"], start=1):
        where = f"{path.name} step {number}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        _only_keys(entry, {"say", "tools", "delay_ms"}, where)
        text = entry.get("say", "")
        tools = entry.get("tools", [])
        delay = entry.get("delay_ms", 0)
        if not isinstance(text, str) or not isinstance(tools, list):
            raise ValueError(f"{where}: 'say' must be a string and 'tools' a list")
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 0:
            raise ValueError(f"{where}: 'delay_ms' must be a whole number of milliseconds")
        parts: list[Part] = [say(text)] if text else []
        for call in tools:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("name"), str)
                or not isinstance(call.get("args"), str)
            ):
                raise ValueError(f"{where}: each tool needs a 'name' and raw JSON 'args' string")
            parts.append(tool(call["name"], call["args"]))
        steps.append(step(*parts, delay_ms=delay))
    return FakeModel(steps, spec=catalog.SCRIPTED_SPEC, loop=data.get("loop", False) is True)


# -- resolvers ---------------------------------------------------------------------


class FakeResolver(ModelResolver):
    """Serves one fixed model to every Turn, or one model per ref."""

    def __init__(self, models: ModelPort | Mapping[str, ModelPort]) -> None:
        if isinstance(models, Mapping):
            self._models: dict[str, ModelPort] = dict(models)
            self._single: ModelPort | None = None
        else:
            self._models = {models.spec.ref: models}
            self._single = models
        if not self._models:
            raise ValueError("FakeResolver needs at least one model")

    def resolve(self, ref: str | None, project_default: str | None) -> ModelPort:
        if self._single is not None:
            return self._single
        key = ref if ref is not None else project_default
        if key is None:
            return next(iter(self._models.values()))
        if key not in self._models:
            raise ModelUnavailable("unknown_model", f"Unknown model {key!r}.")
        return self._models[key]

    def catalog(self) -> Sequence[ModelInfo]:
        return tuple(ModelInfo.from_spec(m.spec, available=True) for m in self._models.values())

    async def health(self, *, probe: bool = False) -> HealthReport:
        return _fake_health(next(iter(self._models)))


class RecordingModels(ModelResolver):
    """Wraps a resolver and records every model it serves into one cassette."""

    def __init__(self, inner: ModelResolver, path: Path) -> None:
        self._inner = inner
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")  # one cassette per run

    def resolve(self, ref: str | None, project_default: str | None) -> ModelPort:
        return RecordingModel(self._inner.resolve(ref, project_default), self.path)

    def catalog(self) -> Sequence[ModelInfo]:
        return self._inner.catalog()

    async def health(self, *, probe: bool = False) -> HealthReport:
        return await self._inner.health(probe=probe)


class ReplayModels(ModelResolver):
    """Serves one cassette to every Turn; each ref's spec comes from the catalog."""

    def __init__(self, path: Path, *, strict: bool = True) -> None:
        self._cassette = Cassette.load(path)
        self._strict = strict

    def resolve(self, ref: str | None, project_default: str | None) -> ModelPort:
        key = ref if ref is not None else project_default
        if key is None:
            recorded = self._cassette.models()
            if not recorded:
                raise ModelUnavailable("model_unavailable", "The cassette records no model.")
            key = recorded[0]
        return ReplayModel(self._cassette, spec=catalog.spec(key), strict=self._strict)

    def catalog(self) -> Sequence[ModelInfo]:
        return tuple(
            ModelInfo.from_spec(catalog.spec(ref), available=True)
            for ref in self._cassette.models()
        )

    async def health(self, *, probe: bool = False) -> HealthReport:
        recorded = self._cassette.models()
        return _fake_health(recorded[0] if recorded else None)


def _fake_health(default_model: str | None) -> HealthReport:
    return HealthReport(
        ok=True,
        default_model=default_model,
        providers=(ProviderHealth("fake", configured=True, reachable=True, detail=None),),
        issues=(),
    )


# -- recording and replay ------------------------------------------------------------


class ReplayMismatch(AssertionError):
    """A request differs from the recorded one; `diff` is a unified diff of the two."""

    def __init__(self, message: str, diff: str) -> None:
        super().__init__(f"{message}\n{diff}" if diff else message)
        self.diff = diff


class RecordingModel(ModelPort):
    """Passes calls to `inner` and appends each request and its events to `path`."""

    def __init__(self, inner: ModelPort, path: Path) -> None:
        self.spec = inner.spec
        self._inner = inner
        self._path = path

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        request = _request(call)
        events: list[dict[str, Any]] = []
        inner = self._inner.stream(call)
        try:
            async for event in inner:
                events.append(_event_json(event))
                yield event
        except ProviderError as error:
            events.append(_error_json(error))
            raise
        finally:
            await _aclose(inner)
            self._append(
                {
                    "hash": _hash(request),
                    "model": self.spec.ref,
                    "request": request,
                    "events": events,
                }
            )

    async def count_tokens(self, call: ModelCall) -> int | None:
        return await self._inner.count_tokens(call)

    def _append(self, line: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


class Cassette:
    """The recorded requests of one run, served in order and shared by its models."""

    def __init__(self, entries: Sequence[Mapping[str, Any]], *, name: str = "cassette") -> None:
        self.name = name
        self._entries = [entry for entry in entries if "hash" in entry]
        self._used = [False] * len(self._entries)

    @classmethod
    def load(cls, path: Path) -> Cassette:
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls([json.loads(line) for line in lines if line.strip()], name=path.name)

    def models(self) -> list[str]:
        """The model refs the cassette records, in first-use order."""
        seen: dict[str, None] = {}
        for entry in self._entries:
            model = entry.get("model")
            if isinstance(model, str):
                seen.setdefault(model)
        return list(seen)

    def take(self, model: str, request: Mapping[str, Any], *, strict: bool) -> Mapping[str, Any]:
        """The entry for `request`: strictly the next one, else the first with its hash."""
        unused = [i for i, used in enumerate(self._used) if not used]
        if not unused:
            raise ReplayMismatch(
                f"{self.name} has no recorded request left", _diff(None, model, request)
            )
        index = unused[0]
        entry = self._entries[index]
        if not _matches(entry, model, request):
            if strict:
                raise ReplayMismatch(
                    f"request {index + 1} differs from {self.name}", _diff(entry, model, request)
                )
            index = next((i for i in unused if _matches(self._entries[i], model, request)), index)
            entry = self._entries[index]
        self._used[index] = True
        return entry


class ReplayModel(ModelPort):
    """Serves recorded events; a request whose hash differs fails with `ReplayMismatch`."""

    def __init__(self, path: Path | Cassette, *, spec: ModelSpec, strict: bool = True) -> None:
        self.spec = spec
        self.calls: list[ModelCall] = []
        self._cassette = path if isinstance(path, Cassette) else Cassette.load(path)
        self._strict = strict

    async def stream(self, call: ModelCall) -> AsyncIterator[ModelEvent]:
        self.calls.append(call)
        entry = self._cassette.take(self.spec.ref, _request(call), strict=self._strict)
        for data in entry["events"]:
            if data["type"] == "error":
                raise ProviderError(
                    data["code"],
                    data["message"],
                    retryable=data["retryable"],
                    retry_after_s=data["retry_after_s"],
                )
            yield _event_from(data)

    async def count_tokens(self, call: ModelCall) -> int | None:
        return None


def _matches(entry: Mapping[str, Any], model: str, request: Mapping[str, Any]) -> bool:
    return entry["hash"] == _hash(request) and entry.get("model", model) == model


def _diff(entry: Mapping[str, Any] | None, model: str, request: Mapping[str, Any]) -> str:
    recorded = [] if entry is None else _render(entry.get("model", model), entry["request"])
    lines = difflib.unified_diff(
        recorded, _render(model, request), "cassette", "request", lineterm=""
    )
    return "\n".join(lines)


def _render(model: str, request: Mapping[str, Any]) -> list[str]:
    """The request one field per section, text written out so a prompt change diffs by line."""
    lines = ["=== model", model, "=== system", *request["system"].split("\n"), "=== tools"]
    lines.extend(_canonical(spec) for spec in request["tools"])
    for index, message in enumerate(request["messages"]):
        lines.append(f"=== messages[{index}] {message['role']}")
        for block in message["blocks"]:
            if block["type"] == "text":
                lines.extend(block["text"].split("\n"))
            elif block["type"] == "tool_use":
                lines.append(f"tool_use {block['id']} {block['name']} {_canonical(block['input'])}")
            else:
                error = " error" if block["is_error"] else ""
                lines.append(f"tool_result {block['tool_use_id']}{error}")
                lines.extend(block["content"].split("\n"))
    lines += ["=== max_output_tokens", str(request["max_output_tokens"])]
    lines += ["=== response_schema", _canonical(request["response_schema"])]
    return lines


# -- JSON forms ------------------------------------------------------------------------


def _request(call: ModelCall) -> dict[str, Any]:
    return {
        "system": call.system,
        "tools": [
            {"name": t.name, "description": t.description, "schema": _plain(t.input_schema)}
            for t in call.tools
        ],
        "messages": [
            {"role": m.role, "blocks": [_block_json(b) for b in m.blocks]} for m in call.messages
        ],
        "max_output_tokens": call.max_output_tokens,
        "response_schema": _plain(call.response_schema),
    }


def _hash(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(request).encode()).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plain(value: Any) -> Any:
    """`value` with every Mapping a dict and every tuple a list, ready for JSON."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _block_json(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": _plain(block.input),
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    assert_never(block)


def _block_from(data: Mapping[str, Any]) -> Block:
    if data["type"] == "text":
        return TextBlock(data["text"])
    if data["type"] == "tool_use":
        return ToolUseBlock(data["id"], data["name"], data["input"])
    return ToolResultBlock(data["tool_use_id"], data["content"], data["is_error"])


def _message_json(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "blocks": [_block_json(b) for b in message.blocks],
        "native": _plain(message.native),
    }


def _message_from(data: Mapping[str, Any]) -> Message:
    return Message(data["role"], tuple(_block_from(b) for b in data["blocks"]), data["native"])


def _usage_json(usage: StepUsage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "served_model": usage.served_model,
        "iterations": [_usage_json(u) for u in usage.iterations],
    }


def _usage_from(data: Mapping[str, Any]) -> StepUsage:
    return StepUsage(
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        cache_read_tokens=data["cache_read_tokens"],
        cache_write_tokens=data["cache_write_tokens"],
        served_model=data["served_model"],
        iterations=tuple(_usage_from(u) for u in data["iterations"]),
    )


def _event_json(event: ModelEvent) -> dict[str, Any]:
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ThinkingStarted):
        return {"type": "thinking_started"}
    if isinstance(event, ToolCallCompleted):
        return {
            "type": "tool_call",
            "id": event.id,
            "name": event.name,
            "raw_args": event.raw_args,
            "args": _plain(event.args),
            "parse_error": event.parse_error,
            "recovered": event.recovered,
        }
    if isinstance(event, UsageReport):
        return {"type": "usage", "usage": _usage_json(event.usage)}
    if isinstance(event, Stopped):
        return {
            "type": "stopped",
            "reason": event.reason,
            "message": _message_json(event.message),
            "refusal_category": event.refusal_category,
        }
    if isinstance(event, ProviderNotice):
        return {"type": "notice", "code": event.code, "message": event.message}
    assert_never(event)


def _event_from(data: Mapping[str, Any]) -> ModelEvent:
    kind = data["type"]
    if kind == "text_delta":
        return TextDelta(data["text"])
    if kind == "thinking_started":
        return ThinkingStarted()
    if kind == "tool_call":
        return ToolCallCompleted(
            id=data["id"],
            name=data["name"],
            raw_args=data["raw_args"],
            args=data["args"],
            parse_error=data["parse_error"],
            recovered=data["recovered"],
        )
    if kind == "usage":
        return UsageReport(_usage_from(data["usage"]))
    if kind == "stopped":
        return Stopped(data["reason"], _message_from(data["message"]), data["refusal_category"])
    if kind == "notice":
        return ProviderNotice(data["code"], data["message"])
    raise ValueError(f"unknown cassette event type: {kind!r}")


def _error_json(error: ProviderError) -> dict[str, Any]:
    return {
        "type": "error",
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "retry_after_s": error.retry_after_s,
    }


# -- helpers ---------------------------------------------------------------------------


def _at(parts: Sequence[Part], index: int) -> Part | None:
    return parts[index] if index < len(parts) else None


def _object_or_none(raw: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _call_chars(call: ModelCall) -> int:
    return len(call.system) + sum(len(text) for text in _texts(call.messages))


def _texts(messages: Sequence[Message]) -> Iterator[str]:
    for message in messages:
        for block in message.blocks:
            if isinstance(block, TextBlock):
                yield block.text
            elif isinstance(block, ToolUseBlock):
                yield _canonical(_plain(block.input))
            else:
                yield block.content


def _only_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(unknown)}")


async def _aclose(stream: AsyncIterator[ModelEvent]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()
