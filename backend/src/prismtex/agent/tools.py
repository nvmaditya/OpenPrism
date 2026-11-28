"""Agent tools (agent design §2.2 `tools`, §4.2).

`ToolSpec` describes one tool to a model. The ToolBox that runs tools builds on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["ToolSpec"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool as a model sees it.

    `input_schema` is a JSON Schema object with `additionalProperties: false`, every
    field required and optional ones nullable. `latex_fields` names the arguments that
    carry LaTeX, which the escape guard checks (agent design §4.6).
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    read_only: bool
    latex_fields: frozenset[str] = frozenset()
