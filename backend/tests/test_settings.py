from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from prismtex.settings import Settings, is_loopback_host


def test_defaults() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.data_dir == Path("data")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.access_token is None
    assert settings.token is None
    assert settings.allowed_hosts == ()
    assert settings.allowed_origins == ()
    assert settings.compile_timeout_s == 120.0
    assert settings.max_upload_mb == 50
    assert settings.import_max_mb == 200
    assert settings.import_max_files == 5000


def test_environment_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PRISMTEX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRISMTEX_HOST", "0.0.0.0")
    monkeypatch.setenv("PRISMTEX_PORT", "9001")
    monkeypatch.setenv("PRISMTEX_ACCESS_TOKEN", "s3cret")
    monkeypatch.setenv("PRISMTEX_ALLOWED_HOSTS", " Box.LAN , ,prismtex.local,")
    monkeypatch.setenv("PRISMTEX_ALLOWED_ORIGINS", "http://box.lan:3000/,https://x.example")
    monkeypatch.setenv("PRISMTEX_COMPILE_TIMEOUT_S", "30.5")
    monkeypatch.setenv("PRISMTEX_MAX_UPLOAD_MB", "5")
    monkeypatch.setenv("PRISMTEX_IMPORT_MAX_MB", "7")
    monkeypatch.setenv("PRISMTEX_IMPORT_MAX_FILES", "9")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.data_dir == tmp_path
    assert settings.host == "0.0.0.0"
    assert settings.port == 9001
    assert settings.token == "s3cret"
    assert "s3cret" not in repr(settings)
    assert settings.allowed_hosts == ("box.lan", "prismtex.local")
    assert settings.allowed_origins == ("http://box.lan:3000", "https://x.example")
    assert settings.compile_timeout_s == 30.5
    assert (settings.max_upload_mb, settings.import_max_mb, settings.import_max_files) == (5, 7, 9)


def test_dotenv_in_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(
        b"PRISMTEX_PORT=8123\nPRISMTEX_ALLOWED_HOSTS=a.example\nPRISMTEX_ACCESS_TOKEN=\n"
        b"OTHER_TOOL_SETTING=ignored\n"
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.port == 8123
    assert settings.allowed_hosts == ("a.example",)
    # A blank token in .env means "no token", never "the empty token".
    assert settings.access_token is None


def test_environment_beats_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"PRISMTEX_PORT=8123\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRISMTEX_PORT", "8124")
    assert Settings().port == 8124


def test_empty_lists_and_blank_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRISMTEX_ALLOWED_HOSTS", "")
    monkeypatch.setenv("PRISMTEX_ACCESS_TOKEN", "   ")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.allowed_hosts == ()
    assert settings.access_token is None


@pytest.mark.parametrize(
    ("name", "value"),
    [("PRISMTEX_PORT", "0"), ("PRISMTEX_MAX_UPLOAD_MB", "0"), ("PRISMTEX_PORT", "http")],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("127.255.255.254", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
        ("10.0.0.1", False),
        ("example.com", False),
        ("localhost.example.com", False),
        ("128.0.0.1", False),
        ("", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert is_loopback_host(host) is expected
