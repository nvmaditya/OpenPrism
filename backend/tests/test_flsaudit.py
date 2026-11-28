"""The .fls audit evaluates the TeX trees in the compile's own environment."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from prismtex.compile import flsaudit


@pytest.fixture
def fake_kpsewhich(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str]]]:
    """kpsewhich that puts TEXMFVAR under $HOME, recording each query's env."""
    calls: list[dict[str, str]] = []

    def run(argv: Sequence[str], *, env: Mapping[str, str], **_: Any) -> Any:
        calls.append(dict(env))
        var = argv[1].removeprefix("-var-value=")
        home = env.get("HOME", "")
        stdout = f"{home}/texmf-var\n" if var == "TEXMFVAR" and home else "\n"
        return subprocess.CompletedProcess(list(argv), 0, stdout=stdout, stderr="")

    monkeypatch.setattr(flsaudit.subprocess, "run", run)
    monkeypatch.setattr(flsaudit.shutil, "which", lambda name, path=None: "/fake/kpsewhich")
    monkeypatch.setattr(flsaudit, "_roots_cache", {})
    yield calls


def _home(tmp_path: Path, name: str) -> Path:
    home = tmp_path / name
    (home / "texmf-var" / "fonts" / "pk").mkdir(parents=True)
    return home


def test_user_trees_expand_from_the_compile_environment(
    tmp_path: Path, fake_kpsewhich: list[dict[str, str]]
) -> None:
    alice, bob = _home(tmp_path, "alice"), _home(tmp_path, "bob")
    roots_a = flsaudit._texmf_roots("/fake/kpsewhich", {"PATH": "/x", "HOME": str(alice)})
    roots_b = flsaudit._texmf_roots("/fake/kpsewhich", {"PATH": "/x", "HOME": str(bob)})
    assert str((alice / "texmf-var").resolve()) in roots_a
    assert str((bob / "texmf-var").resolve()) in roots_b
    assert all(call.get("HOME") for call in fake_kpsewhich)


def test_the_per_run_output_folder_does_not_defeat_the_cache(
    tmp_path: Path, fake_kpsewhich: list[dict[str, str]]
) -> None:
    home = _home(tmp_path, "home")
    for out in ("run-1", "run-2"):
        env = {"PATH": "/x", "HOME": str(home), "TEXMFOUTPUT": str(tmp_path / out)}
        flsaudit._texmf_roots("/fake/kpsewhich", env)
    assert len(fake_kpsewhich) == len(flsaudit._TEXMF_VARS)
    assert all("TEXMFOUTPUT" not in call for call in fake_kpsewhich)


def test_a_font_generated_into_the_users_texmf_var_is_not_flagged(
    tmp_path: Path, fake_kpsewhich: list[dict[str, str]]
) -> None:
    home = _home(tmp_path, "home")
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    font = home / "texmf-var" / "fonts" / "pk" / "cmr10.600pk"
    font.write_bytes(b"pk")
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    (src / "main.tex").write_text("x")
    (out / "main.fls").write_text(
        f"PWD {src}\nINPUT main.tex\nINPUT {font}\nINPUT {secret}\n", encoding="utf-8"
    )

    offending = flsaudit.audit_inputs(out, src, env={"PATH": "/x", "HOME": str(home)})

    assert offending == [str(secret)]


def test_only_the_exact_texmf_cnf_files_kpathsea_lists_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TeX Live keeps its main texmf.cnf above every tree, and the recorder lists it.
    install = tmp_path / "texlive" / "2026"
    install.mkdir(parents=True)
    cnf = install / "texmf.cnf"
    cnf.write_text("% config\n")
    sibling = install / "release-texlive.txt"
    sibling.write_text("not a tree\n")

    def run(argv: Sequence[str], **_: Any) -> Any:
        stdout = f"{cnf}\n" if list(argv[1:]) == ["-all", "texmf.cnf"] else "\n"
        return subprocess.CompletedProcess(list(argv), 0, stdout=stdout, stderr="")

    monkeypatch.setattr(flsaudit.subprocess, "run", run)
    monkeypatch.setattr(flsaudit.shutil, "which", lambda name, path=None: "/fake/kpsewhich")
    monkeypatch.setattr(flsaudit, "_roots_cache", {})
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (out / "main.fls").write_text(f"PWD {src}\nINPUT {cnf}\nINPUT {sibling}\n", encoding="utf-8")

    assert flsaudit.audit_inputs(out, src, env={"PATH": "/x"}) == [str(sibling)]
