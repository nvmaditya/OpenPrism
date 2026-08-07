"""Unit tests for path sandbox pure helpers."""

from pathlib import Path

import pytest

from app.sandbox.paths import PathEscapeError, is_within, resolve_under


def test_resolve_under_rejects_dotdot(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_under(root, "../secret")
    with pytest.raises(PathEscapeError):
        resolve_under(root, "a/../../secret")


def test_resolve_under_rejects_absolute(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_under(root, "/etc/passwd")
    with pytest.raises(PathEscapeError):
        resolve_under(root, r"C:\Windows\system32\drivers")
    with pytest.raises(PathEscapeError):
        resolve_under(root, "C:/Windows/system32")


def test_resolve_under_ok(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    p = resolve_under(root, "figs/a.png")
    assert is_within(root, p)
    assert p == (root / "figs" / "a.png").resolve()


def test_looks_absolute():
    from app.sandbox.paths import looks_absolute

    assert looks_absolute("/etc/passwd") is True
    assert looks_absolute("/abs/dir/") is True
    assert looks_absolute(r"C:\Windows\system32") is True
    assert looks_absolute("C:/Windows/system32") is True
    assert looks_absolute("prism-uploads/x.bin") is False
    assert looks_absolute("../x") is False
