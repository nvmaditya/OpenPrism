"""The Biber guard: which control files it lets biber run on, and how it runs biber.

These are pure-Python checks, so they run in the fast lane on every platform;
the TeX lane runs the guard end to end through latexmk and real biber.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from prismtex.compile import biberguard
from prismtex.compile.biberguard import DataSourceRejected, check_bcf

BCF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<bcf:controlfile version="3.10" bltxversion="3.19"'
    ' xmlns:bcf="https://sourceforge.net/projects/biblatex">\n'
    '  <bcf:bibdata section="0">\n'
    "    {sources}\n"
    "  </bcf:bibdata>\n"
    "</bcf:controlfile>\n"
)


def _source(name: str, *, type_: str = "file", datatype: str = "bibtex") -> str:
    return (
        f'<bcf:datasource type="{type_}" datatype="{datatype}" glob="false">{name}</bcf:datasource>'
    )


def _bcf(tmp_path: Path, *sources: str) -> Path:
    path = tmp_path / "main.bcf"
    path.write_text(BCF.format(sources="\n    ".join(sources)), encoding="utf-8")
    return path


@pytest.mark.parametrize("name", ["refs.bib", "bib/refs.bib", "my refs.bib", "réf.bib"])
def test_project_relative_sources_are_allowed(tmp_path: Path, name: str) -> None:
    check_bcf(str(_bcf(tmp_path, _source(name), _source("other.bib"))))


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "C:\\Users\\me\\secret.bib",
        "C:/Users/me/secret.bib",
        "\\\\server\\share\\x.bib",
        "../outside.bib",
        "bib/../../outside.bib",
        "bib\\..\\..\\outside.bib",
        "http://example.com/x.bib",
        "HTTPS://example.com/x.bib",
        "ftp://example.com/x.bib",
        "file:///etc/passwd",
    ],
)
def test_outside_and_remote_sources_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(DataSourceRejected):
        check_bcf(str(_bcf(tmp_path, _source("refs.bib"), _source(name))))


@pytest.mark.parametrize(
    "datatype",
    [
        "bibtex;system(q(touch x));1",
        "bibtex`touch x`",
        "bibtex x",
        "bibtex::x",
        "bibtex$x",
    ],
)
def test_attribute_values_outside_the_allowlist_are_refused(tmp_path: Path, datatype: str) -> None:
    # biber evals the datatype as a Perl package name; anything but a plain
    # identifier is how the code execution gets in.
    with pytest.raises(DataSourceRejected):
        check_bcf(str(_bcf(tmp_path, _source("refs.bib", datatype=datatype))))


def test_an_unparsable_control_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "main.bcf"
    path.write_text("<bcf:controlfile", encoding="utf-8")
    with pytest.raises(DataSourceRejected):
        check_bcf(str(path))


def test_a_missing_control_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DataSourceRejected):
        check_bcf(str(tmp_path / "absent.bcf"))


def test_main_refuses_without_a_control_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert biberguard.main(["--output-directory", "out"]) == 2
    assert "no .bcf" in capsys.readouterr().err


def test_main_refuses_an_unsafe_control_file_without_running_biber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(biberguard, "_run_biber", lambda *a: ran.append(a) or 0)
    bcf = _bcf(tmp_path, _source("/etc/passwd"))
    assert biberguard.main([str(bcf)]) == 2
    assert ran == []
    assert "refusing to run biber" in capsys.readouterr().err


def test_main_runs_biber_with_noconf_on_a_safe_control_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(biberguard.shutil, "which", lambda name: f"/opt/tex/{name}")

    def fake_run(biber: str, args: list[str]) -> int:
        ran.append((biber, args))
        return 0

    monkeypatch.setattr(biberguard, "_run_biber", fake_run)
    bcf = _bcf(tmp_path, _source("refs.bib"))
    assert biberguard.main(["--output-directory", "out", str(bcf)]) == 0
    assert ran == [("/opt/tex/biber", ["--noconf", "--output-directory", "out", str(bcf)])]


def test_on_windows_biber_runs_as_a_child_and_its_status_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows exec starts a new process and ends the guard with status 0 at
    # once; the guard must wait for biber and pass its status back instead.
    calls: list[list[str]] = []

    class Done:
        returncode = 7

    def fake_run(argv: list[str], check: bool) -> Done:
        calls.append(argv)
        return Done()

    def no_exec(*_: object) -> None:
        raise AssertionError("execv must not be used on Windows")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(biberguard.subprocess, "run", fake_run)
    monkeypatch.setattr(biberguard.os, "execv", no_exec)
    assert biberguard._run_biber("C:/tex/biber.exe", ["--noconf", "main.bcf"]) == 7  # pyright: ignore[reportPrivateUsage]
    assert calls == [["C:/tex/biber.exe", "--noconf", "main.bcf"]]
