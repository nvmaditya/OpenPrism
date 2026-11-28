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
    assert settings.ledger_path is None
    # The agent and its providers (agent design §11.7).
    assert settings.default_model is None
    assert settings.ollama_url == "http://127.0.0.1:11434"
    assert settings.ollama_num_ctx is None
    assert settings.anthropic_api_key is None
    assert settings.anthropic_fallbacks == "off"
    assert (settings.openai_preset, settings.openai_model) == (None, None)
    assert (settings.openai_api_key, settings.openai_base_url) == (None, None)
    assert (settings.openai_context, settings.openai_price) == (32_768, None)
    assert (settings.max_usd_per_turn, settings.cost_stop_usd) == (1.0, None)
    assert settings.trace_prompts is False
    assert settings.fake_script is None


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
    monkeypatch.setenv("PRISMTEX_LEDGER_PATH", str(tmp_path / "claude-ledger.jsonl"))
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
    assert settings.ledger_path == tmp_path / "claude-ledger.jsonl"


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
    monkeypatch.setenv("PRISMTEX_LEDGER_PATH", " ")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.allowed_hosts == ()
    assert settings.access_token is None
    assert settings.ledger_path is None


def test_agent_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRISMTEX_DEFAULT_MODEL", " vllm:qwen ")
    monkeypatch.setenv("PRISMTEX_OLLAMA_URL", " http://127.0.0.1:11500 ")
    monkeypatch.setenv("PRISMTEX_OLLAMA_NUM_CTX", "12288")
    monkeypatch.setenv("PRISMTEX_ANTHROPIC_FALLBACKS", "Default")
    monkeypatch.setenv("PRISMTEX_OPENAI_PRESET", " VLLM ")
    monkeypatch.setenv("PRISMTEX_OPENAI_MODEL", "qwen")
    monkeypatch.setenv("PRISMTEX_OPENAI_API_KEY", "sk-v")
    monkeypatch.setenv("PRISMTEX_OPENAI_BASE_URL", "http://127.0.0.1:8001/v1")
    monkeypatch.setenv("PRISMTEX_OPENAI_CONTEXT", "65536")
    monkeypatch.setenv("PRISMTEX_OPENAI_PRICE", " 0.4 , 1.6 ")
    monkeypatch.setenv("PRISMTEX_MAX_USD_PER_TURN", "0.5")
    monkeypatch.setenv("PRISMTEX_COST_STOP_USD", "9.50")
    monkeypatch.setenv("PRISMTEX_TRACE_PROMPTS", "1")
    monkeypatch.setenv("PRISMTEX_FAKE_SCRIPT", str(tmp_path / "script.json"))
    monkeypatch.setenv("PRISMTEX_ANTHROPIC_API_KEY", "sk-ant-prefixed")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.default_model == "vllm:qwen"
    assert settings.ollama_url == "http://127.0.0.1:11500"
    assert settings.ollama_num_ctx == 12_288
    assert settings.anthropic_fallbacks == "default"
    assert (settings.openai_preset, settings.openai_model) == ("vllm", "qwen")
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-v"
    assert settings.openai_base_url == "http://127.0.0.1:8001/v1"
    assert (settings.openai_context, settings.openai_price) == (65_536, (0.4, 1.6))
    assert (settings.max_usd_per_turn, settings.cost_stop_usd) == (0.5, 9.5)
    assert settings.trace_prompts is True
    assert settings.fake_script == tmp_path / "script.json"
    # populate_by_name also accepts PRISMTEX_ANTHROPIC_API_KEY, after the two plain names.
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-prefixed"
    assert "sk-v" not in repr(settings) and "sk-ant-prefixed" not in repr(settings)
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-claude")
    alias = Settings(_env_file=None).anthropic_api_key  # pyright: ignore[reportCallIssue]
    assert alias is not None and alias.get_secret_value() == "sk-ant-claude"


def test_blank_agent_settings_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PRISMTEX_DEFAULT_MODEL",
        "PRISMTEX_OLLAMA_NUM_CTX",
        "PRISMTEX_OPENAI_PRESET",
        "PRISMTEX_OPENAI_MODEL",
        "PRISMTEX_OPENAI_API_KEY",
        "PRISMTEX_OPENAI_BASE_URL",
        "PRISMTEX_OPENAI_PRICE",
        "PRISMTEX_COST_STOP_USD",
        "PRISMTEX_FAKE_SCRIPT",
        "PRISMTEX_ANTHROPIC_FALLBACKS",
        "PRISMTEX_OLLAMA_URL",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(name, " ")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    unset = (
        settings.default_model,
        settings.ollama_num_ctx,
        settings.openai_preset,
        settings.openai_model,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.openai_price,
        settings.cost_stop_usd,
        settings.fake_script,
        settings.anthropic_api_key,
    )
    assert unset == (None,) * len(unset)
    assert settings.anthropic_fallbacks == "off"
    assert settings.ollama_url == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PRISMTEX_OPENAI_PRESET", "groq"),
        ("PRISMTEX_OPENAI_PRICE", "1"),
        ("PRISMTEX_OPENAI_PRICE", "1,2,3"),
        ("PRISMTEX_OPENAI_PRICE", "-1,2"),
        ("PRISMTEX_OPENAI_PRICE", "nan,2"),
        ("PRISMTEX_ANTHROPIC_FALLBACKS", "on"),
        ("PRISMTEX_OLLAMA_NUM_CTX", "32"),
        ("PRISMTEX_OPENAI_CONTEXT", "0"),
        ("PRISMTEX_MAX_USD_PER_TURN", "-1"),
    ],
)
def test_invalid_agent_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


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
