"""Allowlisted subprocess runner (cwd = project root)."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Product plan / tools.md surface
TEX_ALLOWLIST: frozenset[str] = frozenset(
    {
        "latexmk",
        "pdflatex",
        "xelatex",
        "lualatex",
        "latex",
        "bibtex",
        "biber",
        "makeindex",
        "dvipdfmx",
        "kpsewhich",
    }
)

_ALLOWLIST_LOWER = {t.lower(): t for t in TEX_ALLOWLIST}


class ToolNotAllowlisted(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"tool not allowlisted: {name}")
        self.name = name
        self.code = "not_allowlisted"


class ToolNotFound(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"tool not found on PATH: {name}")
        self.name = name
        self.code = "tool_not_found"


@dataclass(frozen=True)
class RunResult:
    tool: str
    argv: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    executable: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def combined_log(self) -> str:
        parts = [
            f"$ {' '.join(self.argv)}",
            f"(cwd={self.cwd})",
            f"(exe={self.executable})",
            f"(exit={self.returncode}, {self.duration_ms}ms)",
            "",
            "--- stdout ---",
            self.stdout.rstrip(),
            "",
            "--- stderr ---",
            self.stderr.rstrip(),
            "",
        ]
        return "\n".join(parts) + "\n"


def resolve_allowlisted_executable(name: str) -> Path:
    """Resolve a bare tool name if it is on the allowlist and on PATH."""
    if not name or name != name.strip():
        raise ToolNotAllowlisted(name)
    # Reject path-like names (absolute or relative paths)
    if "/" in name or "\\" in name or ".." in name:
        raise ToolNotAllowlisted(name)
    if Path(name).is_absolute():
        raise ToolNotAllowlisted(name)

    base = Path(name).name
    if base != name:
        raise ToolNotAllowlisted(name)

    canonical = _ALLOWLIST_LOWER.get(base.lower())
    if canonical is None:
        raise ToolNotAllowlisted(base)

    exe = shutil.which(canonical)
    if not exe:
        raise ToolNotFound(canonical)
    return Path(exe)


def run_allowlisted(
    tool: str,
    args: list[str],
    *,
    cwd: Path,
    timeout_s: float = 120.0,
    env: dict[str, str] | None = None,
) -> RunResult:
    """
    Run an allowlisted TeX-related binary with ``cwd`` as the working directory.

    ``args`` must be simple tokens (no shell). ``tool`` is a bare name from the allowlist.
    """
    exe = resolve_allowlisted_executable(tool)
    clean_args = [str(a) for a in args]
    argv = [str(exe), *clean_args]
    cwd = Path(cwd).resolve()
    if not cwd.is_dir():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            shell=False,
            check=False,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return RunResult(
            tool=tool,
            argv=[Path(exe).stem, *clean_args],
            cwd=str(cwd),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=duration_ms,
            executable=str(exe),
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else (
            exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        )
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else (
            exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        )
        return RunResult(
            tool=tool,
            argv=[Path(exe).stem, *clean_args],
            cwd=str(cwd),
            returncode=-1,
            stdout=out,
            stderr=(err + f"\nERROR: timeout after {timeout_s}s").strip(),
            duration_ms=duration_ms,
            executable=str(exe),
        )
