"""Verification of staged changes (agent design §2.2 `verify`, §7).

A `Verifier` compiles a Turn's Baseline tree (the Project as the Turn found
it) and its Proposal tree (the staged text) through a `TreeCompiler`, and
compares their problems: a Proposal problem is new when no Baseline problem
matches it (`diff_diagnostics`, §7.3). It judges the verdict (§7.4) and, when
a round finds new problems, first makes the deterministic host repairs of
§7.5 through Staging with `by="host"`, then compiles once more.

Problems are error-severity Diagnostics, the warnings `undefined_citation`
and `undefined_reference`, and `no_pdf` when the Baseline made a PDF and the
Proposal did not. Other warnings only count toward `new_warnings`; `info`
Diagnostics (bad boxes) are ignored.

A compile whose output Phase 0 withheld after a read outside the project (a
single `access_denied` error, docs/compile.md) hides every other Diagnostic.
It is a new problem when the Baseline was not withheld; when the Baseline
was, the comparison cannot be trusted and the verdict is `unverified`
(`masked`) unless the Proposal has no problem at all. It is never repaired.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

from prismtex.agent.events import CompileEvent, DecisionRecord, DiagnosticJSON, DiagnosticsEvent
from prismtex.agent.latex import (
    apply_edits,
    bib_keys,
    ensure_package,
    packages_needed,
    strip_fragment_wrappers,
    wire_bibliography,
)
from prismtex.agent.types import Verdict
from prismtex.cancel import Cancelled, CancelToken
from prismtex.compile.compiler import CompileResult
from prismtex.compile.logparse import Diagnostic
from prismtex.compile.tree import SourceTree, TreeCompiler
from prismtex.errors import AppError, RootFileInvalid
from prismtex.outline import Outline, build_outline

if TYPE_CHECKING:
    from prismtex.agent.proposals import LineMap
    from prismtex.agent.staging import StagedFile, Staging, TurnBase
    from prismtex.agent.trace import TraceWriter

__all__ = [
    "NO_PDF_MESSAGE",
    "DiagnosticDelta",
    "DiagnosticView",
    "Severity",
    "UnverifiedReason",
    "Verification",
    "Verifier",
    "diagnostic_view",
    "diagnostic_views",
    "diff_diagnostics",
    "fingerprint",
]

Severity = Literal["error", "warning", "info"]
UnverifiedReason = Literal["cancelled", "tex_unavailable", "timeout", "masked", "no_root_file"]
Stage = Literal["baseline", "proposal"]

NO_PDF_MESSAGE: Final = "No PDF was produced."
# Warnings that count as problems: an invented \cite key or \ref must not pass.
_PROBLEM_WARNINGS: Final = frozenset({"undefined_citation", "undefined_reference"})
# A Proposal problem matches a Baseline one this many lines away (§7.3 step 2).
_NEAR_LINES: Final = 2
# An excerpt keeps at most this many characters of its source line.
_EXCERPT_CHARS: Final = 500
# Past the last line of any file: Outline.order clamps it to the file's end.
_END_OF_FILE: Final = 2**31

_SEVERITIES = frozenset(get_args(Severity))
_VERDICTS = frozenset(get_args(Verdict))
_REASONS = frozenset(get_args(UnverifiedReason))
_RANK: Final[Mapping[str, int]] = {"error": 0, "warning": 1, "info": 2}
_DIGITS = re.compile(r"[0-9]+")
# The messages logparse writes for the verifier (agent design §7.3).
_UNDEFINED_COMMAND = re.compile(r"Undefined control sequence (\\[A-Za-z]+)")
_UNDEFINED_ENVIRONMENT = re.compile(r"Environment ([^\s{}\\]+) undefined")
_CITATION_KEY = re.compile(r"Citation [`']([^'\s]+)'")
_WRAPPER = re.compile(r"\\(?:begin|end)\s*\{document\}|\\documentclass(?![A-Za-z])")


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

    `reason` is set only when the verdict is "unverified". `rounds` is the
    Repair round the check ran in (0 before any). `baseline_errors` and
    `errors` count error-severity Diagnostics of the Baseline and of the
    Proposal (`no_pdf` included). `new` and `fixed` are the problems the
    Proposal added and removed, `new` errors first; `new_warnings` counts
    the other warnings it added. `host_fixes` names every host repair made
    in the Turn so far, such as "added \\usepackage{booktabs}"; `pdf` says
    whether the Proposal tree made a PDF.
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


@dataclass(frozen=True)
class DiagnosticDelta:
    """How the Proposal's problems compare with the Baseline's (agent design §7.3).

    `new` holds the Proposal problems no Baseline problem matches, errors
    first; `fixed` the Baseline problems nothing matched; `persisting` the
    matched Proposal problems. Each otherwise keeps the order it was given.
    """

    new: tuple[DiagnosticView, ...]
    fixed: tuple[DiagnosticView, ...]
    persisting: tuple[DiagnosticView, ...]


# -- pure functions -------------------------------------------------------------------


def diagnostic_view(diagnostic: Diagnostic, texts: Mapping[str, str]) -> DiagnosticView:
    """A Diagnostic with the source line it points at, whitespace collapsed (agent design §7.3).

    `texts` are the LF texts of the tree that was compiled, by path. The
    excerpt is None when the Diagnostic has no line, or its file or line is
    not in `texts`; it keeps at most 500 characters.
    """
    excerpt: str | None = None
    if diagnostic.file is not None and diagnostic.line is not None:
        text = texts.get(diagnostic.file)
        if text is not None:
            lines = text.split("\n")
            if lines[-1] == "":
                lines.pop()
            if 1 <= diagnostic.line <= len(lines):
                excerpt = _collapse(lines[diagnostic.line - 1])[:_EXCERPT_CHARS]
    return DiagnosticView(
        file=diagnostic.file,
        line=diagnostic.line,
        severity=diagnostic.severity,
        kind=diagnostic.kind,
        message=diagnostic.message,
        excerpt=excerpt,
    )


def diagnostic_views(result: CompileResult, tree: SourceTree) -> tuple[DiagnosticView, ...]:
    """The Diagnostics of `result`, a compile of `tree`, each with its excerpt."""
    texts = _texts(tree)
    return tuple(diagnostic_view(d, texts) for d in result.diagnostics)


def fingerprint(d: DiagnosticView) -> str:
    """sha256 hex over the file, the kind, the message and the excerpt (agent design §7.3).

    Digit runs in the message are masked as `#` and the excerpt's whitespace
    is collapsed. It holds no line number and no severity, so a problem an
    edit only moved keeps its fingerprint.
    """
    excerpt = None if d.excerpt is None else _collapse(d.excerpt)
    payload = json.dumps([d.file, d.kind, _masked(d.message), excerpt], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_diagnostics(
    baseline: Sequence[DiagnosticView],
    proposal: Sequence[DiagnosticView],
    maps: Mapping[str, LineMap],
) -> DiagnosticDelta:
    """Match Proposal problems to Baseline problems (agent design §7.3).

    1. A multiset match by fingerprint.
    2. A Proposal problem still unmatched takes an unmatched Baseline problem
       with the same file, kind and digit-masked message whose line, mapped
       through `maps[file]`, is at most 2 lines from it (the nearest wins), or
       else whose line lies in a replaced Hunk while the Proposal line lies
       in that Hunk's new lines. A file without a map is unchanged.
    3. Unmatched Proposal problems are new, unmatched Baseline problems fixed.
    """
    base = list(baseline)
    ours = list(proposal)
    free = [True] * len(base)
    matched = [False] * len(ours)

    pool: dict[str, list[int]] = {}
    for index, item in enumerate(base):
        pool.setdefault(fingerprint(item), []).append(index)
    for index, item in enumerate(ours):
        waiting = pool.get(fingerprint(item))
        if waiting:
            free[waiting.pop(0)] = False
            matched[index] = True

    keys = [_match_key(item) for item in base]
    for index, item in enumerate(ours):
        if matched[index] or item.file is None or item.line is None:
            continue
        found = _nearby(item, item.line, base, keys, free, maps.get(item.file))
        if found is not None:
            free[found] = False
            matched[index] = True

    new = sorted(
        (item for index, item in enumerate(ours) if not matched[index]),
        key=lambda item: _RANK[item.severity],
    )
    return DiagnosticDelta(
        new=tuple(new),
        fixed=tuple(item for index, item in enumerate(base) if free[index]),
        persisting=tuple(item for index, item in enumerate(ours) if matched[index]),
    )


def _match_key(item: DiagnosticView) -> tuple[str | None, str, str]:
    return item.file, item.kind, _masked(item.message)


def _nearby(
    item: DiagnosticView,
    line: int,
    base: Sequence[DiagnosticView],
    keys: Sequence[tuple[str | None, str, str]],
    free: Sequence[bool],
    line_map: LineMap | None,
) -> int | None:
    """The Baseline problem rule 2 of `diff_diagnostics` matches `item` with, if any."""
    key = _match_key(item)
    nearest: tuple[int, int] | None = None  # (distance, index)
    in_hunk: int | None = None
    for index, other in enumerate(base):
        if not free[index] or other.line is None or keys[index] != key:
            continue
        mapped = other.line if line_map is None else line_map.to_new(other.line)
        if mapped is not None:
            distance = abs(mapped - line)
            if distance <= _NEAR_LINES and (nearest is None or distance < nearest[0]):
                nearest = (distance, index)
        elif in_hunk is None and line_map is not None and _same_hunk(line_map, other.line, line):
            in_hunk = index
    return nearest[1] if nearest is not None else in_hunk


def _same_hunk(line_map: LineMap, base_line: int, new_line: int) -> bool:
    """Whether a replaced base line and a new line lie in the same Hunk.

    The Hunks are the gaps between the unchanged runs of the map; the gap
    after the last run is open-ended, and a map without runs is one Hunk.
    """
    base_from, new_from = 1, 1  # the first line of the current gap on each side
    for start, end, offset in sorted(line_map.runs):
        if base_from <= base_line < start and new_from <= new_line < start + offset:
            return True
        base_from, new_from = end + 1, end + offset + 1
    return base_line >= base_from and new_line >= new_from


# -- the verifier ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Compiled:
    """One compile as the verifier reads it; `reason` is set when it cannot be used."""

    reason: UnverifiedReason | None
    views: tuple[DiagnosticView, ...] = ()
    pdf: bool = False
    withheld: bool = False  # Phase 0 withheld the output after a read outside the project
    texts: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def errors(self) -> int:
        return sum(d.severity == "error" for d in self.views)


@dataclass(frozen=True)
class _Judged:
    delta: DiagnosticDelta
    errors: int
    new_warnings: int


@dataclass(frozen=True)
class _Changes:
    """Where the Proposal differs from the base.

    `maps` holds a LineMap per changed path; `starts` the first changed base
    line of each changed file of the base.
    """

    maps: Mapping[str, LineMap]
    starts: tuple[tuple[str, int], ...]


class Verifier:
    """Compiles a Turn's Baseline and Proposal trees and judges the difference (agent design §7).

    `prime` starts the Baseline compile as a task, once; `check` waits for
    it, compiles the Proposal tree, and returns the Verification. `close`
    cancels an unfinished Baseline compile; finalization calls it. Compiles
    pass `turn_id=trace.turn_id` and `timeout_s`, and each one is announced
    with `compile` events; every Proposal compile that could be compared is
    followed by a `diagnostics` event. A TurnBase without a Root file is
    never compiled. Call every method on the Turn's event loop.
    """

    def __init__(
        self,
        compiler: TreeCompiler,
        base: TurnBase,
        trace: TraceWriter,
        cancel: CancelToken,
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self._compiler = compiler
        self._base = base
        self._trace = trace
        self._cancel = cancel
        self._timeout_s = timeout_s
        self._baseline: asyncio.Task[_Compiled] | None = None
        self._closed = False
        self._host_fixes: list[str] = []
        self._base_outline: Outline | None = None

    def prime(self) -> None:
        """Start the Baseline compile; idempotent, and a no-op with no Root file or after close."""
        if self._baseline is not None or self._closed or self._base.root_file is None:
            return
        self._baseline = asyncio.get_running_loop().create_task(self._compile_baseline())

    async def check(self, staging: Staging, round: int) -> Verification:
        """Compile the Proposal tree of `staging` and compare it with the Baseline.

        `round` is the Repair round (0 for the first check). When the
        comparison finds new problems, the host repairs run once and the
        Proposal tree is compiled once more. Never raises for a compile that
        failed, timed out, was refused or was cancelled.
        """
        if self._base.root_file is None:
            return self._unverified("no_root_file", round)
        if self._cancel.is_set():
            return self._unverified("cancelled", round)
        self.prime()
        baseline = await self._wait_for_baseline()
        if baseline.reason is not None:
            return self._unverified(baseline.reason, round)

        tree = await asyncio.to_thread(staging.tree)
        proposal = await self._compile(tree, "proposal", round)
        if proposal.reason is not None:
            return self._unverified(proposal.reason, round, baseline)
        changes = _changes(staging.files())
        masked = self._masked(baseline, changes)
        judged = _judge(baseline, proposal, changes.maps)
        self._announce(round, judged)

        repairable = not (masked or baseline.withheld or proposal.withheld)
        if judged.delta.new and repairable:
            fixes = self._repair(staging, proposal, judged.delta.new, round)
            if fixes:
                self._host_fixes.extend(f for f in fixes if f not in self._host_fixes)
                tree = await asyncio.to_thread(staging.tree)
                proposal = await self._compile(tree, "proposal", round)
                if proposal.reason is not None:
                    return self._unverified(proposal.reason, round, baseline)
                changes = _changes(staging.files())
                judged = _judge(baseline, proposal, changes.maps)
                self._announce(round, judged)

        untrusted = masked or (baseline.withheld and (proposal.withheld or bool(judged.delta.new)))
        verdict: Verdict = (
            "unverified" if untrusted else "regressed" if judged.delta.new else "clean"
        )
        return Verification(
            verdict=verdict,
            reason="masked" if untrusted else None,
            rounds=round,
            baseline_errors=baseline.errors,
            errors=judged.errors,
            new=judged.delta.new,
            fixed=judged.delta.fixed,
            new_warnings=judged.new_warnings,
            host_fixes=tuple(self._host_fixes),
            pdf=proposal.pdf,
        )

    async def close(self) -> None:
        """Cancel an unfinished Baseline compile and wait for it to stop; idempotent.

        `prime` does nothing afterwards. A later `check` still compares with
        a Baseline that had finished, and is `unverified` (`cancelled`)
        otherwise.
        """
        self._closed = True
        task = self._baseline
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved, so asyncio never reports it as lost

    # -- compiles -----------------------------------------------------------------

    async def _compile_baseline(self) -> _Compiled:
        tree = await asyncio.to_thread(self._base.tree)
        return await self._compile(tree, "baseline", 0)

    async def _wait_for_baseline(self) -> _Compiled:
        task = self._baseline
        if task is None:  # closed before it started
            return _Compiled("cancelled")
        await asyncio.wait({task})
        if task.cancelled():
            return _Compiled("cancelled")
        return task.result()

    async def _compile(self, tree: SourceTree, stage: Stage, round: int) -> _Compiled:
        """Compile `tree`, from the cache when `peek` has it, with its `compile` events."""
        if self._cancel.is_set():
            return _Compiled("cancelled")
        texts = _texts(tree)
        cached = await asyncio.to_thread(self._compiler.peek, tree.digest)
        if cached is not None:
            errors, warnings = _counts(cached)
            self._trace.emit(
                CompileEvent(
                    stage=stage,
                    round=round,
                    state="cached",
                    ok=cached.ok,
                    errors=errors,
                    warnings=warnings,
                )
            )
            return _read(cached, texts)
        self._trace.emit(CompileEvent(stage=stage, round=round, state="started"))
        refusal: AppError | None = None
        try:
            result = await self._compiler.compile(
                tree,
                purpose=stage,
                cancel=self._cancel,
                timeout_s=self._timeout_s,
                turn_id=self._trace.turn_id,
            )
        except Cancelled:
            return _Compiled("cancelled")
        except AppError as exc:  # refused before TeX ran: no Root file, an unsafe file
            refusal = exc
            result = _refused(exc, tree)
        errors, warnings = _counts(result)
        self._trace.emit(
            CompileEvent(
                stage=stage,
                round=round,
                state="finished",
                ok=result.ok,
                errors=errors,
                warnings=warnings,
                duration_ms=result.duration_ms,
            )
        )
        compiled = _read(result, texts)
        if refusal is not None and stage == "baseline":
            # Nothing to compare with. In a Proposal the refusal is the Turn's problem.
            reason: UnverifiedReason = (
                "no_root_file" if isinstance(refusal, RootFileInvalid) else "tex_unavailable"
            )
            return replace(compiled, reason=reason)
        return compiled

    # -- judging ------------------------------------------------------------------

    def _masked(self, baseline: _Compiled, changes: _Changes) -> bool:
        """A Baseline `fatal` lies before the first changed line in document order.

        A `fatal` with no file is not located and masks nothing; one with no
        line counts as the end of its file.
        """
        fatals = [d for d in baseline.views if d.kind == "fatal" and d.file is not None]
        if not fatals or not changes.starts:
            return False
        outline = self._outline_of_base()
        first = min(outline.order(path, line) for path, line in changes.starts)
        for fatal in fatals:
            assert fatal.file is not None
            line = fatal.line if fatal.line is not None else _END_OF_FILE
            if outline.order(fatal.file, line) < first:
                return True
        return False

    def _outline_of_base(self) -> Outline:
        if self._base_outline is None:
            files = self._base.files
            root = self._base.root_file
            assert root is not None
            self._base_outline = build_outline(lambda path: files[path].text, list(files), root)
        return self._base_outline

    def _announce(self, round: int, judged: _Judged) -> None:
        delta = judged.delta
        self._trace.emit(
            DiagnosticsEvent(
                round=round,
                new=tuple(_json(d) for d in delta.new),
                fixed=tuple(_json(d) for d in delta.fixed),
                persisting_errors=sum(d.severity == "error" for d in delta.persisting),
                host_fixes=tuple(self._host_fixes),
            )
        )

    def _unverified(
        self, reason: UnverifiedReason, round: int, baseline: _Compiled | None = None
    ) -> Verification:
        return Verification(
            verdict="unverified",
            reason=reason,
            rounds=round,
            baseline_errors=baseline.errors if baseline is not None else 0,
            errors=0,
            new=(),
            fixed=(),
            new_warnings=0,
            host_fixes=tuple(self._host_fixes),
            pdf=False,
        )

    # -- host repairs (§7.5) ----------------------------------------------------------

    def _repair(
        self, staging: Staging, proposal: _Compiled, new: Sequence[DiagnosticView], round: int
    ) -> list[str]:
        """Make every host repair the new problems call for; the notes of those made."""
        root = self._base.root_file
        assert root is not None
        texts = proposal.texts
        outline = build_outline(texts.__getitem__, list(texts), root)
        notes = [
            *_add_packages(staging, root, outline, new),
            *_strip_wrappers(staging, root, new),
            *_wire_bibliography(staging, root, outline, new),
        ]
        for note in notes:
            self._trace.emit(
                DecisionRecord(kind="host_repair", choice=note, detail={"round": round})
            )
        return notes


def _add_packages(
    staging: Staging, root: str, outline: Outline, new: Sequence[DiagnosticView]
) -> list[str]:
    """`ensure_package` for each undefined command or environment that `packages.json` knows."""
    loaded = frozenset(outline.packages)
    wanted: dict[str, None] = {}
    for item in new:
        probe = _package_probe(item)
        if probe is not None:
            wanted.update(dict.fromkeys(packages_needed(probe, loaded)))
    notes: list[str] = []
    for name in wanted:
        text = _read_text(staging, root)
        if text is None:
            break
        try:
            edit = ensure_package(text, name, root_file=root)
        except ValueError:
            continue
        if (
            edit is not None
            and edit.lines
            and _host_write(staging, root, apply_edits(text, [edit]))
        ):
            notes.append(edit.note)
    return notes


def _package_probe(item: DiagnosticView) -> str | None:
    """The LaTeX that names what an undefined-command or -environment problem lacks."""
    if item.kind == "undefined_control_sequence":
        match = _UNDEFINED_COMMAND.fullmatch(item.message)
        return match[1] if match else None
    if item.kind == "undefined_environment":
        match = _UNDEFINED_ENVIRONMENT.fullmatch(item.message)
        return f"\\begin{{{match[1]}}}" if match else None
    return None


def _strip_wrappers(staging: Staging, root: str, new: Sequence[DiagnosticView]) -> list[str]:
    """`strip_fragment_wrappers` on the lines the Proposal puts in an included file.

    For each file other than the Root file with a new error that names
    `\\documentclass`, `\\begin{document}` or `\\end{document}` in its message
    or its line. Lines the file had in the base are never touched.
    """
    paths = dict.fromkeys(
        item.file
        for item in new
        if item.severity == "error"
        and item.file is not None
        and item.file != root
        and (_WRAPPER.search(item.message) or _WRAPPER.search(item.excerpt or ""))
    )
    if not paths:
        return []
    files = {f.path: f for f in staging.files()}
    notes: list[str] = []
    for path in paths:
        staged = files.get(path)
        if staged is None or staged.staged is None:
            continue  # the Proposal does not change this file
        base_text = staged.base.text if staged.base is not None else ""
        text, removed = _strip_added_lines(base_text, staged.staged)
        if removed and _host_write(staging, path, text):
            notes.append(f"removed {_listed(dict.fromkeys(removed))} from {path}")
    return notes


def _strip_added_lines(base_text: str, text: str) -> tuple[str, tuple[str, ...]]:
    """`text` with the wrappers removed from the lines it does not share with `base_text`."""
    from prismtex.agent.proposals import join_lines, split_lines  # proposals imports this module

    base_lines, lines = split_lines(base_text), split_lines(text)
    out: list[str] = []
    removed: list[str] = []
    matcher = difflib.SequenceMatcher(None, base_lines, lines, autojunk=False)
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        segment = lines[j1:j2]
        if tag != "equal" and segment:
            stripped, cut = strip_fragment_wrappers("\n".join(segment) + "\n")
            if cut:
                removed.extend(cut)
                segment = split_lines(stripped)
        out.extend(segment)
    return join_lines(out, like=text), tuple(removed)


def _wire_bibliography(
    staging: Staging, root: str, outline: Outline, new: Sequence[DiagnosticView]
) -> list[str]:
    """`wire_bibliography` for a `.bib` file the document does not read.

    Only for a file to which the Proposal added the key of a new undefined
    citation.
    """
    keys = {
        match[1]
        for item in new
        if item.kind == "undefined_citation" and (match := _CITATION_KEY.search(item.message))
    }
    if not keys:
        return []
    notes: list[str] = []
    for staged in staging.files():
        if (
            not staged.path.lower().endswith(".bib")
            or staged.staged is None
            or staged.path in outline.bib_files
        ):
            continue
        before = bib_keys(staged.base.text) if staged.base is not None else ()
        added = set(bib_keys(staged.staged)).difference(before)
        if added.isdisjoint(keys):
            continue
        text = _read_text(staging, root)
        if text is None:
            break
        try:
            edits = [
                e for e in wire_bibliography(text, outline, staged.path, root_file=root) if e.lines
            ]
        except ValueError:
            continue  # not a .bib name TeX can read
        if edits and _host_write(staging, root, apply_edits(text, edits)):
            notes.extend(edit.note for edit in edits)
    return notes


def _read_text(staging: Staging, path: str) -> str | None:
    from prismtex.agent.staging import ToolError  # staging imports proposals, which imports this

    try:
        return staging.read(path).text
    except ToolError:
        return None


def _host_write(staging: Staging, path: str, text: str) -> bool:
    """Stage a host repair; False when Staging refuses it or nothing changed."""
    from prismtex.agent.staging import ToolError  # staging imports proposals, which imports this

    try:
        change = staging.write(path, text, by="host")
    except ToolError:
        return False
    return change.span is not None


# -- helpers --------------------------------------------------------------------------

_NO_PDF = DiagnosticView(None, None, "error", "no_pdf", NO_PDF_MESSAGE, None)


def _read(result: CompileResult, texts: Mapping[str, str]) -> _Compiled:
    reason: UnverifiedReason | None = None
    if result.status == "unavailable":
        reason = "tex_unavailable"
    elif result.status == "timeout":
        reason = "timeout"
    return _Compiled(
        reason=reason,
        views=tuple(diagnostic_view(d, texts) for d in result.diagnostics),
        pdf=result.pdf_path is not None,
        withheld=any(
            d.kind == "access_denied" and d.severity == "error" for d in result.diagnostics
        ),
        texts=texts,
    )


def _refused(exc: AppError, tree: SourceTree) -> CompileResult:
    """A failed result for a compile refused before TeX ran."""
    return CompileResult(
        status="failed",
        diagnostics=(Diagnostic(None, None, "error", exc.code, exc.message),),
        pdf_path=None,
        log_text=exc.message + "\n",
        duration_ms=0,
        engine=tree.engine,
        root_file=tree.root_file,
        exit_code=None,
    )


def _judge(baseline: _Compiled, proposal: _Compiled, maps: Mapping[str, LineMap]) -> _Judged:
    problems = [d for d in proposal.views if _is_problem(d)]
    if baseline.pdf and not proposal.pdf:
        problems.append(_NO_PDF)
    delta = diff_diagnostics([d for d in baseline.views if _is_problem(d)], problems, maps)
    warnings = diff_diagnostics(
        _other_warnings(baseline.views), _other_warnings(proposal.views), maps
    )
    return _Judged(delta, sum(d.severity == "error" for d in problems), len(warnings.new))


def _changes(files: Sequence[StagedFile]) -> _Changes:
    """The line maps from the base to the staged text, and where each file first changes.

    A map's runs are the equal opcodes of the same diff `proposals.compute`
    makes, so they are the runs `proposals.line_map` gives for the Proposal's
    FileChange. A created or deleted file maps no line.
    """
    from prismtex.agent.proposals import LineMap, split_lines  # proposals imports this module

    maps: dict[str, LineMap] = {}
    starts: list[tuple[str, int]] = []
    for staged in files:
        if staged.base is None or staged.staged is None:
            maps[staged.path] = LineMap(staged.path, ())
            if staged.base is not None:
                starts.append((staged.path, 1))
            continue
        base_lines, lines = split_lines(staged.base.text), split_lines(staged.staged)
        opcodes = difflib.SequenceMatcher(None, base_lines, lines, autojunk=False).get_opcodes()
        changed = [i1 for tag, i1, _, _, _ in opcodes if tag != "equal"]
        if not changed:
            continue
        runs = tuple((i1 + 1, i2, j1 - i1) for tag, i1, i2, j1, _ in opcodes if tag == "equal")
        maps[staged.path] = LineMap(staged.path, runs)
        # A change at index i (0-based) touches base line i + 1, or is inserted before it.
        starts.append((staged.path, changed[0] + 1))
    return _Changes(maps, tuple(starts))


def _is_problem(d: DiagnosticView) -> bool:
    return d.severity == "error" or (d.severity == "warning" and d.kind in _PROBLEM_WARNINGS)


def _other_warnings(views: Iterable[DiagnosticView]) -> list[DiagnosticView]:
    return [d for d in views if d.severity == "warning" and d.kind not in _PROBLEM_WARNINGS]


def _texts(tree: SourceTree) -> dict[str, str]:
    """The text of each Source file of the tree; Baseline and Proposal trees overlay them all."""
    return {path: text for path, text in tree.overlay.items() if text is not None}


def _counts(result: CompileResult) -> tuple[int, int]:
    errors = sum(d.severity == "error" for d in result.diagnostics)
    warnings = sum(d.severity == "warning" for d in result.diagnostics)
    return errors, warnings


def _json(d: DiagnosticView) -> DiagnosticJSON:
    return DiagnosticJSON(
        file=d.file,
        line=d.line,
        severity=d.severity,
        kind=d.kind,
        message=d.message,
        excerpt=d.excerpt,
    )


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _masked(message: str) -> str:
    return _DIGITS.sub("#", message)


def _listed(items: Iterable[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    names = list(items)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


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
