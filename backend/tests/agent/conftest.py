"""Fixtures for the agent tests: Traces, ToolBoxes and Turn scopes under a temporary folder."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from prismtex.agent.budget import DEFAULT_LIMITS, BudgetGuard, Ledger, Limits
from prismtex.agent.context import ContextAssembler, PromptSet, TokenEstimator
from prismtex.agent.events import Envelope, Event
from prismtex.agent.providers import ModelPort
from prismtex.agent.providers.fake import fake_spec
from prismtex.agent.staging import Staging, capture_base
from prismtex.agent.steps import StepRunner
from prismtex.agent.text import known_commands
from prismtex.agent.threads import ThreadMemory
from prismtex.agent.tools import ToolBox
from prismtex.agent.trace import Manifest, TraceWriter
from prismtex.agent.turn import TurnScope
from prismtex.agent.types import EditorContext, Intent, TurnRequest, Usage
from prismtex.agent.verify import DiagnosticView, Verification
from prismtex.cancel import CancelToken
from prismtex.outline import Outline, build_outline
from prismtex.workspace import ProjectStore

MakeTrace = Callable[..., TraceWriter]
Check = Callable[[], Awaitable[Verification]]

CLEAN = Verification("clean", None, 0, 0, 0, (), (), 0, (), True)


def manifest_for(project_id: str = "p1", turn_id: str = "t_1", thread_id: str = "th_1") -> Manifest:
    return Manifest(
        turn_id=turn_id,
        thread_id=thread_id,
        project_id=project_id,
        state="accepted",
        model="fake:m",
        profile="agentic",
        prompt_version="test",
        started_at="2026-11-25T00:00:00Z",
        finished_at=None,
        status=None,
        outcome=None,
        verdict=None,
        summary=None,
        proposal_id=None,
        usage=Usage(),
        error=None,
    )


@pytest.fixture
def make_trace(tmp_path: Path) -> MakeTrace:
    """A TraceWriter under `tmp_path`; keyword arguments override manifest fields."""

    def make(project_id: str = "p1", turn_id: str = "t_1", **manifest: Any) -> TraceWriter:
        base = replace(manifest_for(project_id, turn_id), **manifest)
        return TraceWriter(tmp_path, project_id, turn_id, manifest=base)

    return make


@pytest.fixture
def trace(make_trace: MakeTrace) -> TraceWriter:
    return make_trace()


class RecordingTrace(TraceWriter):
    """A TraceWriter that also keeps every emitted event, in order, in `events`."""

    def __init__(
        self, data_dir: Path, project_id: str, turn_id: str, *, manifest: Manifest
    ) -> None:
        self.events: list[Event] = []
        super().__init__(data_dir, project_id, turn_id, manifest=manifest)

    def emit(self, event: Event, *, internal: bool = False) -> Envelope:
        self.events.append(event)
        return super().emit(event, internal=internal)


def staged_outline(staging: Staging) -> Callable[[], Outline]:
    """The Outline of the staged text, built afresh on every call."""

    def outline() -> Outline:
        def read(path: str) -> str:
            return staging.read(path).text

        return build_outline(read, staging.paths(), staging.base.root_file or "main.tex")

    return outline


def staged_commands(staging: Staging) -> frozenset[str]:
    texts: list[str] = []
    for path in staging.paths():
        if path.endswith(".tex"):
            try:
                texts.append(staging.read(path).text)
            except Exception:
                continue
    return known_commands(texts)


async def clean_check() -> Verification:
    return CLEAN


MakeToolbox = Callable[..., ToolBox]


@pytest.fixture
def make_toolbox() -> MakeToolbox:
    """A ToolBox over `staging`: the staged Outline, a clean `check`, a free budget.

    `check` and `limits` replace the clean check and the agentic limits.
    """

    def make(
        staging: Staging,
        *,
        intent: Intent = "edit",
        check: Check | None = None,
        limits: Limits | None = None,
    ) -> ToolBox:
        budget = BudgetGuard(
            limits or DEFAULT_LIMITS["agentic"],
            fake_spec,
            None,
            max_usd_per_turn=1.0,
            cost_stop_usd=None,
        )
        return ToolBox(
            staging,
            staged_outline(staging),
            check or clean_check,
            budget,
            staged_commands(staging),
            intent,
        )

    return make


@dataclass
class ScopeKit:
    """A TurnScope and what the test needs to look at: every event, and Baseline primes."""

    scope: TurnScope
    trace: RecordingTrace
    budget: BudgetGuard
    primed: list[int] = field(default_factory=list[int])

    @property
    def events(self) -> list[Event]:
        return self.trace.events


MakeScope = Callable[..., ScopeKit]


@pytest.fixture
def make_scope(tmp_path: Path) -> MakeScope:
    """A TurnScope with real Staging, StepRunner, ContextAssembler and ToolBox.

    Events go to a RecordingTrace, so `kit.events` holds the StepRunner's and
    the Profile's events in emission order. A paid model gets a ledger under
    `tmp_path`.
    """
    counter = [0]

    def make(
        store: ProjectStore,
        pid: str,
        model: ModelPort,
        *,
        intent: Intent,
        editor: EditorContext | None = None,
        message: str = "Do the thing",
        memory: ThreadMemory | None = None,
        diagnostics: Sequence[DiagnosticView] = (),
        check: Check | None = None,
        limits: Limits | None = None,
    ) -> ScopeKit:
        counter[0] += 1
        turn_id = f"t_{counter[0]}"
        spec = model.spec
        staging, _ = Staging.open(capture_base(store, pid, editor), None)
        trace = RecordingTrace(tmp_path, pid, turn_id, manifest=manifest_for(pid, turn_id))
        ledger = Ledger(tmp_path / "ledger.jsonl") if spec.paid else None
        budget = BudgetGuard(
            limits or DEFAULT_LIMITS["agentic"],
            spec,
            ledger,
            max_usd_per_turn=1.0,
            cost_stop_usd=None,
            project_id=pid,
            turn_id=turn_id,
        )
        cancel = CancelToken()
        context = ContextAssembler(PromptSet.load(), TokenEstimator(spec.chars_per_token))
        steps = StepRunner(model, context, budget, trace, cancel)
        tools = ToolBox(
            staging,
            staged_outline(staging),
            check or clean_check,
            budget,
            staged_commands(staging),
            intent,
        )
        primed: list[int] = []
        scope = TurnScope(
            turn_id=turn_id,
            request=TurnRequest(pid, message, editor=editor),
            intent=intent,
            spec=spec,
            steps=steps,
            staging=staging,
            outline=staged_outline(staging),
            diagnostics=tuple(diagnostics),
            context=context,
            memory=memory or ThreadMemory("th_1", (), None, (), ()),
            tools=tools,
            emit=trace.emit,
            prime_baseline=lambda: primed.append(1),
            cancel=cancel,
        )
        return ScopeKit(scope, trace, budget, primed)

    return make
