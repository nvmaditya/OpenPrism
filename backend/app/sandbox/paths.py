"""Resolve and enforce paths inside a root directory."""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a path would leave the allowed root."""


def resolve_under(root: Path, relative: str | Path) -> Path:
    """Resolve ``relative`` under ``root``; reject escapes and absolute paths."""
    root = root.resolve()
    rel = Path(relative)
    if rel.is_absolute():
        raise PathEscapeError(f"absolute paths are not allowed: {relative}")
    # Disallow Windows drive-style and parent segments in the *logical* path
    parts = rel.parts
    if any(p in ("..", "") for p in parts if p == ".."):
        raise PathEscapeError(f"path escapes root: {relative}")
    if ".." in parts:
        raise PathEscapeError(f"path escapes root: {relative}")

    candidate = (root / rel).resolve()
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
