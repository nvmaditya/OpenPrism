"""Fixtures for the agent tests: Traces over a temporary data directory."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from prismtex.agent.trace import Manifest, TraceWriter
from prismtex.agent.types import Usage

MakeTrace = Callable[..., TraceWriter]


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
