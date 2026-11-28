"""A TreeCompiler without TeX, and a CompileResult builder, for tests and evals.

`FakeTreeCompiler` turns regex rules over the effective Source texts of a
tree into Diagnostics, and caches like `ScratchTreeCompiler`, including the
rule that a user compile reuses only an earlier user compile's result.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from prismtex.cancel import CancelToken
from prismtex.compile.compiler import TEX_MISSING_MESSAGE, CompileResult, CompileStatus
from prismtex.compile.logparse import Diagnostic, Severity
from prismtex.compile.tree import Purpose, SourceTree
from prismtex.errors import AppError
from prismtex.workspace import Engine, ProjectStore

__all__ = ["FakeRule", "FakeTreeCompiler", "make_result"]

_EXIT_CODES: dict[str, int | None] = {"ok": 0, "failed": 1, "timeout": None, "unavailable": None}


def make_result(
    *,
    status: CompileStatus = "ok",
    diagnostics: Sequence[Diagnostic] = (),
    pdf_path: Path | str | None = None,
    duration_ms: int = 1,
    root_file: str = "main.tex",
    engine: Engine = "pdflatex",
    log_id: str | None = "l",
    log_text: str = "",
) -> CompileResult:
    """A CompileResult with `status`; the exit code follows from it (0, 1 or None)."""
    return CompileResult(
        status=status,
        diagnostics=tuple(diagnostics),
        pdf_path=Path(pdf_path) if pdf_path is not None else None,
        log_text=log_text,
        duration_ms=duration_ms,
        engine=engine,
        root_file=root_file,
        exit_code=_EXIT_CODES[status],
        log_id=log_id,
    )


@dataclass(frozen=True)
class FakeRule:
    """One Diagnostic per match of `pattern` on a line of a Source text.

    `message` is a format string; `{match}` is the matched text. The rule is
    off when `unless` matches anywhere in the tree. A `fatal` match stops
    the scan after its Diagnostic (a masked Baseline); a `no_pdf` match
    leaves the result without a PDF.
    """

    pattern: str
    kind: str
    message: str
    severity: Severity = "error"
    unless: str | None = None
    fatal: bool = False
    no_pdf: bool = False


class FakeTreeCompiler:
    """A TreeCompiler that applies FakeRules instead of running TeX.

    `calls` records (tree, purpose) for every compile that was not served
    from the cache. With `unavailable=True` every compile is "unavailable",
    as when latexmk is missing, and nothing is cached. The Root file is
    scanned first, then the other Source files in path order.
    """

    def __init__(self, projects: ProjectStore, *rules: FakeRule, unavailable: bool = False) -> None:
        self.projects = projects
        self.rules = rules
        self.unavailable = unavailable
        self.calls: list[tuple[SourceTree, str]] = []
        self._cache: OrderedDict[str, tuple[Purpose, CompileResult]] = OrderedDict()

    async def compile(
        self,
        tree: SourceTree,
        *,
        purpose: Purpose,
        cancel: CancelToken,
        timeout_s: float = 120.0,
        turn_id: str | None = None,
    ) -> CompileResult:
        cancel.raise_if_set()
        cached = self._cache.get(tree.digest)
        if cached is not None and (purpose != "user" or cached[0] == "user"):
            return cached[1]
        self.calls.append((tree, purpose))
        if self.unavailable:
            return make_result(
                status="unavailable",
                diagnostics=(Diagnostic(None, None, "error", "tex_missing", TEX_MISSING_MESSAGE),),
                root_file=tree.root_file,
                engine=tree.engine,
                duration_ms=0,
            )
        result = self._apply(tree, purpose, turn_id)
        self._cache[tree.digest] = (purpose, result)
        return result

    def peek(self, digest: str) -> CompileResult | None:
        cached = self._cache.get(digest)
        return cached[1] if cached is not None else None

    def _apply(self, tree: SourceTree, purpose: Purpose, turn_id: str | None) -> CompileResult:
        texts = self._texts(tree)
        everything = "\n".join(texts.values())
        rules = [r for r in self.rules if r.unless is None or not re.search(r.unless, everything)]
        found, no_pdf = _scan(texts, rules)
        failed = any(d.severity == "error" for d in found)
        return make_result(
            status="failed" if failed else "ok",
            diagnostics=found,
            pdf_path=None if no_pdf else self._pdf_path(tree, purpose, turn_id),
            root_file=tree.root_file,
            engine=tree.engine,
        )

    def _texts(self, tree: SourceTree) -> dict[str, str]:
        """The effective text of each Source file, the Root file first."""
        order = sorted(tree.inputs, key=lambda path: (path != tree.root_file, path))
        texts: dict[str, str] = {}
        for path in order:
            text = tree.overlay.get(path)
            if text is None:
                try:
                    text = self.projects.read(tree.project_id, path).text
                except AppError:
                    continue  # not UTF-8 text, or gone: a binary file has no lines to match
            texts[path] = text
        return texts

    def _pdf_path(self, tree: SourceTree, purpose: Purpose, turn_id: str | None) -> Path:
        """Where the real compiler would put the PDF; the fake writes no file."""
        stem = f"{PurePosixPath(tree.root_file).stem}.pdf"
        layout = self.projects.layout
        if purpose == "user" or turn_id is None:
            return layout.out(tree.project_id) / stem
        return layout.build(tree.project_id) / "scratch" / turn_id / tree.digest / "out" / stem


def _scan(texts: dict[str, str], rules: Sequence[FakeRule]) -> tuple[list[Diagnostic], bool]:
    """The Diagnostics in scan order up to the first fatal one, and whether a no_pdf matched."""
    found: list[Diagnostic] = []
    no_pdf = False
    for path, text in texts.items():
        for number, line in enumerate(text.split("\n"), start=1):
            for rule in rules:
                for match in re.finditer(rule.pattern, line):
                    message = rule.message.format(match=match.group(0))
                    found.append(Diagnostic(path, number, rule.severity, rule.kind, message))
                    no_pdf = no_pdf or rule.no_pdf
                    if rule.fatal:
                        return found, no_pdf
    return found, no_pdf
