"""Resolve and enforce paths inside a root directory."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath


class PathEscapeError(ValueError):
    """Raised when a path would leave the allowed root."""


_ABS_WIN = re.compile(r"^[A-Za-z]:[/\\]")
_UNC = re.compile(r"^\\\\")


def looks_absolute(relative: str | Path) -> bool:
    """True if ``relative`` is an absolute POSIX, Windows, or UNC path."""
    s = str(relative).replace("\\", "/")
    if not s:
        return False
    if s.startswith("/"):
        return True
    if _ABS_WIN.match(str(relative)) or _ABS_WIN.match(s):
        return True
    if _UNC.match(str(relative)):
        return True
    # pathlib absolute on current OS
    if Path(relative).is_absolute():
        return True
    if PureWindowsPath(relative).is_absolute() or PurePosixPath(s).is_absolute():
        return True
    return False


# Back-compat alias
_looks_absolute = looks_absolute


def normalize_rel(relative: str | Path) -> str:
    """Normalize a relative path to posix form without leading ./."""
    s = str(relative).replace("\\", "/").strip()
    if s.startswith("./"):
        s = s[2:]
    return s


def resolve_under(root: Path, relative: str | Path) -> Path:
    """Resolve ``relative`` under ``root``; reject escapes and absolute paths."""
    root = root.resolve()
    if _looks_absolute(relative):
        raise PathEscapeError(f"absolute paths are not allowed: {relative}")

    s = normalize_rel(relative)
    if s in ("", "."):
        return root

    parts = [p for p in s.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathEscapeError(f"path escapes root: {relative}")

    candidate = root
    for part in parts:
        if part == "..":
            raise PathEscapeError(f"path escapes root: {relative}")
        candidate = candidate / part

    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(f"path escapes root: {relative}") from exc
    return candidate


def is_within(root: Path, path: Path) -> bool:
    root = root.resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False
