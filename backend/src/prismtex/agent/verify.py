"""Verification of staged changes (agent design §2.2 `verify`, §7).

For now this module holds only the records a Proposal carries: one Diagnostic
as the agent shows it, and the result of verifying a Proposal tree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from prismtex.agent.types import Verdict

__all__ = ["DiagnosticView", "Severity", "UnverifiedReason", "Verification"]

Severity = Literal["error", "warning", "info"]
UnverifiedReason = Literal["cancelled", "tex_unavailable", "timeout", "masked", "no_root_file"]

_SEVERITIES = frozenset(get_args(Severity))
_VERDICTS = frozenset(get_args(Verdict))
_REASONS = frozenset(get_args(UnverifiedReason))


@dataclass(frozen=True)
class DiagnosticView:
    """A compile Diagnostic plus the source line it points at (`excerpt`)."""

    file: str | None
    line: int | None
    severity: Severity
    kind: str
    message: str
    excerpt: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DiagnosticView:
        """Build from `to_dict` output; raises ValueError on a malformed value."""
        severity = data["severity"]
        if severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        return cls(
            file=_optional_str(data["file"]),
            line=_optional_int(data["line"]),
            severity=severity,
            kind=str(data["kind"]),
            message=str(data["message"]),
            excerpt=_optional_str(data["excerpt"]),
        )


@dataclass(frozen=True)
class Verification:
    """How a Proposal tree compiled compared with the Baseline (agent design §7.4).

    `reason` is set only when the verdict is "unverified". `new` and `fixed`
    are the problems the Proposal added and removed, `new` errors first.
    `host_fixes` names the host repairs made, such as "added
    \\usepackage{booktabs}"; `pdf` says whether the Proposal tree made a PDF.
    """

    verdict: Verdict
    reason: UnverifiedReason | None
    rounds: int
    baseline_errors: int
    errors: int
    new: tuple[DiagnosticView, ...]
    fixed: tuple[DiagnosticView, ...]
    new_warnings: int
    host_fixes: tuple[str, ...]
    pdf: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "rounds": self.rounds,
            "baseline_errors": self.baseline_errors,
            "errors": self.errors,
            "new": [d.to_dict() for d in self.new],
            "fixed": [d.to_dict() for d in self.fixed],
            "new_warnings": self.new_warnings,
            "host_fixes": list(self.host_fixes),
            "pdf": self.pdf,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Verification:
        """Build from `to_dict` output; raises ValueError on a malformed value."""
        verdict = data["verdict"]
        reason = data["reason"]
        if verdict not in _VERDICTS:
            raise ValueError(f"unknown verdict {verdict!r}")
        if reason is not None and reason not in _REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        return cls(
            verdict=verdict,
            reason=reason,
            rounds=_int(data["rounds"]),
            baseline_errors=_int(data["baseline_errors"]),
            errors=_int(data["errors"]),
            new=tuple(DiagnosticView.from_dict(d) for d in _list(data["new"])),
            fixed=tuple(DiagnosticView.from_dict(d) for d in _list(data["fixed"])),
            new_warnings=_int(data["new_warnings"]),
            host_fixes=tuple(str(fix) for fix in _list(data["host_fixes"])),
            pdf=bool(data["pdf"]),
        )


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {type(value).__name__}")
    return value  # pyright: ignore[reportUnknownVariableType]
