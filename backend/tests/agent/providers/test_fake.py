"""The fake, recording and replay adapters (agent design §11.5)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from prismtex.agent.providers import (
    Message,
    ModelCall,
    ModelEvent,
    ModelPort,
    ModelUnavailable,
    ProviderError,
    Stopped,
    TextBlock,
    TextDelta,
    ThinkingStarted,
    ToolCallCompleted,
    ToolResultBlock,
    ToolUseBlock,
)
from prismtex.agent.providers.fake import (
    FakeModel,
    FakeResolver,
    RecordingModel,
    RecordingModels,
    ReplayMismatch,
    ReplayModel,
    ReplayModels,
    ScriptExhausted,
    Step,
    anthropic_like,
    fail,
    fake_spec,
    refuse,
    say,
    scripted_model,
    step,
    tool,
    truncate,
)
from prismtex.agent.tools import ToolSpec

VIEW = ToolSpec(
    "view",
    "View numbered lines.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
    },
    read_only=True,
)


def user(text: str) -> Message:
    return Message("user", (TextBlock(text),))


async def run(model: ModelPort, call: ModelCall) -> list[ModelEvent]:
    return [event async for event in model.stream(call)]


def stopped(events: list[ModelEvent]) -> Stopped:
    last = events[-1]
    assert isinstance(last, Stopped)
    return last


@pytest.mark.anyio
async def test_fake_model_streams_raw_json_args_and_records_every_call() -> None:
    # Raw JSON with single-escaped LaTeX: the fake passes it through untouched.
    raw = '{"path": "main.tex", "old_str": "Hello", "new_str": "Hello \\textbf{Hi}"}'
    model = FakeModel([step(say("Editing."), tool("str_replace", raw)), step(say("Done."))])
    call = ModelCall(system="s", messages=(user("Bold it"),), max_output_tokens=256)
    first = [e async for e in model.stream(call)]
    deltas = [e for e in first if isinstance(e, TextDelta)]
    assert len(deltas) == 2 and "".join(d.text for d in deltas) == "Editing."
    [tc] = [e for e in first if isinstance(e, ToolCallCompleted)]
    assert tc.name == "str_replace" and tc.raw_args == raw and tc.recovered is False
    assert isinstance(first[-1], Stopped) and first[-1].reason == "tool_use"
    with pytest.raises(AssertionError):
        model.assert_consumed()
    second = [e async for e in model.stream(call)]
    assert isinstance(second[-1], Stopped) and second[-1].reason == "end"
    assert model.calls == [call, call]
    model.assert_consumed()
    with pytest.raises(ScriptExhausted):
        [e async for e in model.stream(call)]


@pytest.mark.anyio
async def test_fake_truncate_leaves_the_tool_call_open_with_parse_error_truncated() -> None:
    partial = '{"path": "main.tex", "new_str": "Hel'
    model = FakeModel(
        [
            step(
                say("Fixing."),
                tool("view", '{"path": "main.tex"}'),
                tool("insert", partial),
                truncate(),
            )
        ]
    )
    events = await run(model, ModelCall(system="s", messages=(user("Fix it"),)))
    done, cut = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert (done.parse_error, done.args) == (None, {"path": "main.tex"})
    assert (cut.parse_error, cut.args, cut.raw_args) == ("truncated", None, partial)
    end = stopped(events)
    assert end.reason == "max_tokens"
    assert end.message.blocks[-1] == ToolUseBlock(cut.id, "insert", {})
    with pytest.raises(ValueError, match="last part"):
        step(truncate(), say("more"))


@pytest.mark.anyio
async def test_fake_fail_refuse_and_callable_steps() -> None:
    seen: list[ModelCall] = []

    def echo(call: ModelCall) -> Step:
        seen.append(call)
        return step(say(f"{len(call.messages)} messages"))

    model = FakeModel(
        [fail(ProviderError("rate_limited", retry_after_s=2.0)), refuse("cyber"), echo]
    )
    call = ModelCall(system="s", messages=(user("hi"),))
    with pytest.raises(ProviderError) as caught:
        await run(model, call)
    assert (caught.value.code, caught.value.retryable, caught.value.retry_after_s) == (
        "rate_limited",
        True,
        2.0,
    )
    refusal = stopped(await run(model, call))
    assert (refusal.reason, refusal.refusal_category, refusal.message.blocks) == (
        "refusal",
        "cyber",
        (),
    )
    reply = await run(model, call)
    assert seen == [call] and stopped(reply).message.blocks == (TextBlock("1 messages"),)
    assert model.calls == [call, call, call]


@pytest.mark.anyio
async def test_anthropic_like_fake_streams_thinking_and_opaque_native_payloads() -> None:
    def script() -> FakeModel:
        return FakeModel(
            [step(say("Looking."), tool("view", '{"path": "main.tex"}')), step(say("Done."))],
            spec=anthropic_like,
        )

    model = script()
    call = ModelCall(system="s", messages=(user("Show main.tex"),), tools=(VIEW,))
    first = await run(model, call)
    assert isinstance(first[0], ThinkingStarted)
    native = stopped(first).message.native
    assert native is not None and native["role"] == "assistant"
    thinking, text, use = native["content"]
    assert thinking["type"] == "thinking" and thinking["thinking"] == "" and thinking["signature"]
    assert text == {"type": "text", "text": "Looking."}
    assert (
        use["type"] == "tool_use" and use["name"] == "view" and use["input"] == {"path": "main.tex"}
    )
    second = stopped(await run(model, call)).message.native
    assert second is not None and second["content"][0]["signature"] != thinking["signature"]
    again = stopped(await run(script(), call)).message.native
    assert again == native  # deterministic, so replays and snapshots are stable
    assert stopped(await run(FakeModel([step(say("x"))]), call)).message.native is None


@pytest.mark.anyio
async def test_replay_model_fails_with_a_diff_on_prompt_drift(tmp_path: Path) -> None:
    cassette = tmp_path / "cassettes" / "run.jsonl"
    live_model = FakeModel(
        [step(say("Looking."), tool("view", '{"path": "main.tex"}')), step(say("Done."))]
    )
    recording = RecordingModel(live_model, cassette)
    first = ModelCall(
        system="s", messages=(user("Show main.tex"),), tools=(VIEW,), max_output_tokens=512
    )
    live_first = await run(recording, first)
    [call] = [e for e in live_first if isinstance(e, ToolCallCompleted)]
    second = replace(
        first,
        messages=(
            *first.messages,
            stopped(live_first).message,
            Message("user", (ToolResultBlock(call.id, "1| Hello"),)),
        ),
    )
    live_second = await run(recording, second)
    lines = cassette.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and set(json.loads(lines[0])) == {"hash", "model", "request", "events"}

    replay = ReplayModel(cassette, spec=fake_spec)
    assert await run(replay, first) == live_first
    assert await run(replay, second) == live_second

    drifted = ReplayModel(cassette, spec=fake_spec)
    with pytest.raises(ReplayMismatch) as caught:
        await run(drifted, replace(first, system="s2"))
    diff = caught.value.diff.splitlines()
    assert "-s" in diff and "+s2" in diff and str(caught.value).endswith(caught.value.diff)

    tolerant = ReplayModel(cassette, spec=fake_spec, strict=False)
    assert await run(tolerant, replace(first, system="s2")) == live_first
    with pytest.raises(ReplayMismatch, match="no recorded request left"):
        await run(replay, first)


@pytest.mark.anyio
async def test_recording_and_replay_resolvers_share_one_cassette_across_turns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial.jsonl"
    path.write_text("stale\n", encoding="utf-8")
    inner = FakeModel([step(say("One.")), fail(ProviderError("overloaded"))])
    recorder = RecordingModels(FakeResolver(inner), path)
    assert path.read_text(encoding="utf-8") == ""  # one cassette per run
    call = ModelCall(system="s", messages=(user("hi"),))
    first = await run(recorder.resolve(None, None), call)
    with pytest.raises(ProviderError):
        await run(recorder.resolve(None, None), call)

    replays = ReplayModels(path)
    assert [info.ref for info in replays.catalog()] == ["fake:m"]
    port = replays.resolve(None, None)
    assert port.spec == fake_spec
    assert await run(port, call) == first
    with pytest.raises(ProviderError) as caught:
        await run(replays.resolve(None, "fake:m"), call)  # a second Turn, same cassette
    assert caught.value.code == "overloaded"


@pytest.mark.anyio
async def test_fake_resolver_serves_one_model_or_one_per_ref() -> None:
    model = FakeModel([])
    assert FakeResolver(model).resolve("anthropic:claude-opus-5-5", None) is model
    other = FakeModel([], spec=anthropic_like)
    resolver = FakeResolver({"fake:m": model, "fake:anthropic-like": other})
    assert resolver.resolve(None, None) is model
    assert resolver.resolve(None, "fake:anthropic-like") is other
    assert resolver.resolve("fake:m", "fake:anthropic-like") is model
    with pytest.raises(ModelUnavailable) as caught:
        resolver.resolve("ollama:qwen3:4b-instruct-2507-q4_K_M", None)
    assert caught.value.code == "unknown_model"
    infos = resolver.catalog()
    assert [(i.ref, i.paid, i.available) for i in infos] == [
        ("fake:m", False, True),
        ("fake:anthropic-like", True, True),
    ]
    health = await resolver.health(probe=True)
    assert health.ok and health.default_model == "fake:m" and health.issues == ()


@pytest.mark.anyio
async def test_fake_scripted_model_loops_its_script(tmp_path: Path) -> None:
    raw = '{"path": "main.tex", "old_str": "Hello", "new_str": "Hi"}'
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            {
                "loop": True,
                "steps": [
                    {
                        "say": "Editing.",
                        "tools": [{"name": "str_replace", "args": raw}],
                        "delay_ms": 0,
                    },
                    {"say": "Done."},
                ],
            }
        ),
        encoding="utf-8",
    )
    model = scripted_model(script)
    assert model.spec.ref == "fake:scripted"
    call = ModelCall(system="s", messages=(user("Shorten it"),))
    reasons: list[str] = []
    for _ in range(6):  # three Turns of two Steps each
        events = await run(model, call)
        reasons.append(stopped(events).reason)
        assert all(e.raw_args == raw for e in events if isinstance(e, ToolCallCompleted))
    assert reasons == ["tool_use", "end"] * 3
    model.assert_consumed()

    script.write_text(json.dumps({"steps": [{"say": "x", "tool": []}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown keys: tool"):
        scripted_model(script)
    script.write_text(
        json.dumps({"steps": [{"tools": [{"name": "view", "args": {}}]}]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="raw JSON 'args' string"):
        scripted_model(script)
