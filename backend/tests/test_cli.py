from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from prismtex import cli


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Record uvicorn.run calls instead of serving; keep data and .env lookups in tmp_path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRISMTEX_DATA_DIR", str(tmp_path / "data"))
    calls: list[dict[str, Any]] = []

    def fake_run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return calls


def test_serve_defaults_to_loopback(runs: list[dict[str, Any]], tmp_path: Path) -> None:
    assert cli.main(["serve"]) == 0
    [call] = runs
    assert isinstance(call["app"], FastAPI)
    assert (call["host"], call["port"]) == ("127.0.0.1", 8000)
    assert call["workers"] == 1
    assert call["proxy_headers"] is False
    assert call["server_header"] is False
    assert (tmp_path / "data" / "projects").is_dir()


def test_host_and_port_flags(runs: list[dict[str, Any]]) -> None:
    assert cli.main(["serve", "--host", "localhost", "--port", "9123"]) == 0
    assert (runs[0]["host"], runs[0]["port"]) == ("localhost", 9123)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_refuses_non_loopback_without_token(
    runs: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], host: str
) -> None:
    assert cli.main(["serve", "--host", host]) == 2
    assert runs == []
    err = capsys.readouterr().err
    assert err.strip() == (
        f"Refusing to bind {host} without PRISMTEX_ACCESS_TOKEN. Set a token, or bind 127.0.0.1."
    )


def test_refuses_with_blank_token(
    runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRISMTEX_ACCESS_TOKEN", " ")
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert runs == []


def test_accepts_non_loopback_with_token(
    runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRISMTEX_ACCESS_TOKEN", "s3cret")
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 0
    assert runs[0]["host"] == "0.0.0.0"


def test_host_from_environment_is_checked(
    runs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRISMTEX_HOST", "0.0.0.0")
    assert cli.main(["serve"]) == 2
    assert runs == []


def test_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main([])
    assert info.value.code == 2


def test_python_dash_m(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "prismtex", "--version"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "prismtex 0.1.0.dev0"


def test_harden_process_clears_dumpable_on_linux() -> None:
    if sys.platform != "linux":
        pytest.skip("PR_SET_DUMPABLE is Linux-only")
    import ctypes

    cli._harden_process()
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    # PR_GET_DUMPABLE (3) returns the flag _harden_process cleared with PR_SET_DUMPABLE (4).
    assert libc.prctl(3, 0, 0, 0, 0) == 0


def test_harden_process_is_a_noop_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "platform", "win32")
    cli._harden_process()  # must not raise
