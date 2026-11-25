from __future__ import annotations

import os
from pathlib import Path

import pytest

from prismtex.errors import PathPolicyError, RootFileInvalid
from prismtex.workspace import check_relative_path, check_root_file, is_internal, resolve_in

REJECTED = [
    ("", "empty"),
    (".", "empty"),
    ("./", "empty"),
    ("/", "absolute"),
    ("/etc/passwd", "absolute"),
    ("C:\\Windows\\win.ini", "drive"),
    ("C:/Windows/win.ini", "drive"),
    ("c:x.tex", "drive_relative"),
    ("\\\\server\\share\\x.tex", "unc"),
    ("//server/share", "unc"),
    ("..", "parent"),
    ("../x.tex", "parent"),
    ("a/../../x.tex", "parent"),
    ("a\\..\\b.tex", "parent"),
    ("a\x00b.tex", "control_char"),
    ("a\nb.tex", "control_char"),
    ("a\tb.tex", "control_char"),
    ("a\x1fb.tex", "control_char"),
    ("a<b.tex", "reserved_char"),
    ("a>b.tex", "reserved_char"),
    ('a"b.tex', "reserved_char"),
    ("a|b.tex", "reserved_char"),
    ("a?b.tex", "reserved_char"),
    ("a*b.tex", "reserved_char"),
    ("main.tex:ads", "reserved_char"),
    ("main.tex::$DATA", "reserved_char"),
    ("CON", "device_name"),
    ("con.tex", "device_name"),
    ("Com1.txt", "device_name"),
    ("lpt9", "device_name"),
    ("sections/nul.tex", "device_name"),
    ("aux.tar.gz", "device_name"),
    ("prn", "device_name"),
    ("COM0.log", "device_name"),
    ("com¹.txt", "device_name"),
    # Windows strips trailing spaces before the extension, so these are devices.
    ("con .tex", "device_name"),
    ("nul .bib", "device_name"),
    ("aux  .tex", "device_name"),
    ("COM1 .txt", "device_name"),
    ("sub/aux  .tex", "device_name"),
    ("conin$", "device_name"),
    ("CONOUT$.tex", "device_name"),
    ("name.", "trailing_dot_or_space"),
    ("folder./x.tex", "trailing_dot_or_space"),
    ("name ", "trailing_dot_or_space"),
    ("...", "trailing_dot_or_space"),
    ("a" * 256, "segment_too_long"),
    # 128 two-byte characters is 256 UTF-8 bytes: within 255 code points but
    # over the 255-byte file-system limit.
    ("é" * 128 + ".tex", "segment_too_long"),
    ("中" * 90 + ".tex", "segment_too_long"),
    ("/".join(["abcdefghij"] * 22), "too_long"),
]


@pytest.mark.parametrize(("path", "reason"), REJECTED)
def test_rejected_paths(path: str, reason: str) -> None:
    with pytest.raises(PathPolicyError) as info:
        check_relative_path(path)
    assert info.value.reason == reason
    assert info.value.code == "path_denied"
    assert info.value.extra == {"path": path, "reason": reason}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("main.tex", "main.tex"),
        ("./main.tex", "main.tex"),
        ("sections//intro.tex", "sections/intro.tex"),
        ("sections\\intro.tex", "sections/intro.tex"),
        ("sections/./intro.tex", "sections/intro.tex"),
        ("sections/", "sections"),
        ("my figures/plot 1.png", "my figures/plot 1.png"),
        ("résumé/çà.tex", "résumé/çà.tex"),
        ("日本語/論文.tex", "日本語/論文.tex"),
        (".hidden/file", ".hidden/file"),
        ("console.tex", "console.tex"),
        ("com10.txt", "com10.txt"),
        ("a..b.tex", "a..b.tex"),
        ("a" * 240, "a" * 240),
        # 120 two-byte characters is 240 UTF-8 bytes, within the 255-byte limit.
        ("é" * 120, "é" * 120),
        ("conin.tex", "conin.tex"),
        ("/".join(["abcdefghi"] * 24), "/".join(["abcdefghi"] * 24)),
    ],
)
def test_accepted_paths(path: str, expected: str) -> None:
    assert check_relative_path(path) == expected


def test_is_internal() -> None:
    assert is_internal(".git")
    assert is_internal(".git/config")
    assert is_internal(".PRISM/meta.json")
    assert is_internal(".Git/HEAD")
    assert not is_internal("sub/.git/config")
    assert not is_internal(".github/workflows/ci.yml")


def test_resolve_in_stays_inside(tmp_path: Path) -> None:
    assert resolve_in(tmp_path, "a/b.tex") == (tmp_path / "a" / "b.tex").resolve()
    with pytest.raises(PathPolicyError):
        resolve_in(tmp_path, "../outside.tex")


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def test_resolve_in_refuses_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    _symlink(root / "link", outside)
    with pytest.raises(PathPolicyError) as info:
        resolve_in(root, "link/secret.txt")
    assert info.value.reason == "outside_root"


def test_check_root_file(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_bytes(b"")
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "Paper.TEX").write_bytes(b"")
    (tmp_path / "-x.tex").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    assert check_root_file(tmp_path, "main.tex") == "main.tex"
    assert check_root_file(tmp_path, "./sections/Paper.TEX") == "sections/Paper.TEX"


@pytest.mark.parametrize(
    ("root_file", "reason"),
    [
        ("-x.tex", "leading_dash"),
        ("./-x.tex", "leading_dash"),
        ("notes.txt", "not_tex"),
        ("missing.tex", "not_found"),
        ("sections", "not_tex"),
        ("../main.tex", "path_denied"),
        ("/etc/main.tex", "path_denied"),
        (".git/main.tex", "path_denied"),
        ("my%file.tex", "bad_char"),
        ("a#b.tex", "bad_char"),
        ("a$b.tex", "bad_char"),
        ("a&b.tex", "bad_char"),
        ("a~b.tex", "bad_char"),
        ("a^b.tex", "bad_char"),
        ("a{b}.tex", "bad_char"),
        ("sections\\main.tex", "bad_char"),
        ("a\tb.tex", "bad_char"),
        ("a\u00a0b.tex", "bad_char"),
    ],
)
def test_check_root_file_rejections(tmp_path: Path, root_file: str, reason: str) -> None:
    (tmp_path / "-x.tex").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    (tmp_path / "sections").mkdir()
    with pytest.raises(RootFileInvalid) as info:
        check_root_file(tmp_path, root_file)
    assert info.value.reason == reason
    assert info.value.extra["field"] == "root_file"


def test_root_file_may_contain_spaces(tmp_path: Path) -> None:
    (tmp_path / "my paper.tex").write_bytes(b"")
    assert check_root_file(tmp_path, "my paper.tex") == "my paper.tex"
