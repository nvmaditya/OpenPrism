"""Server configuration: every variable the server reads is a field of `Settings`."""

from __future__ import annotations

import ipaddress
import math
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The OpenAI-compatible presets (agent design §11.3); there is no Groq preset.
OpenAIPreset = Literal["openai", "openrouter", "vllm", "lmstudio"]


class Settings(BaseSettings):
    """Read from the process environment and `.env` in the working directory.

    Keys are `SecretStr` fields, so they never show in a repr. The Anthropic key is read
    from `ANTHROPIC_API_KEY`, then `CLAUDE_API_KEY`, then `PRISMTEX_ANTHROPIC_API_KEY`
    (`populate_by_name` makes the field name a third choice); tests pass
    `anthropic_api_key=...` directly.
    """

    model_config = SettingsConfigDict(
        env_prefix="PRISMTEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    access_token: SecretStr | None = None
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()
    compile_timeout_s: float = Field(default=120.0, gt=0)
    max_upload_mb: int = Field(default=50, gt=0)
    import_max_mb: int = Field(default=200, gt=0)
    import_max_files: int = Field(default=5000, gt=0)
    # The cost ledger of paid model calls (agent design §11.8); None means
    # <data_dir>/state/ledger.jsonl (`prismtex.agent.budget.configured_ledger_path`).
    ledger_path: Path | None = None

    # -- the agent and its model providers (agent design §11.7) --
    # The model ref used when a Project sets none; unset picks the first available.
    default_model: str | None = None
    ollama_url: str = "http://127.0.0.1:11434"
    # Pinned num_ctx for every Ollama model; unset uses the catalog's window.
    ollama_num_ctx: int | None = Field(default=None, gt=32)
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
    )
    anthropic_fallbacks: Literal["off", "default"] = "off"
    # The one configured OpenAI-compatible endpoint: its preset, model, key and URL.
    openai_preset: OpenAIPreset | None = None
    openai_model: str | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    # Window and USD per million tokens (input, output) of a model the catalog lacks;
    # a price meters the model as paid.
    openai_context: int = Field(default=32_768, gt=0)
    openai_price: Annotated[tuple[float, float] | None, NoDecode] = None
    max_usd_per_turn: float = Field(default=1.0, ge=0)
    cost_stop_usd: float | None = Field(default=None, ge=0)
    trace_prompts: bool = False
    # A `fake:scripted` script file; set only by the end-to-end tests.
    fake_script: Path | None = None

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list | tuple):
            # Hostnames and origins compare case-insensitively; a trailing
            # slash on an origin is a common copy-paste slip.
            return tuple(
                str(item).strip().lower().rstrip("/") for item in value if str(item).strip()
            )
        return value

    @field_validator("access_token", mode="before")
    @classmethod
    def _blank_token_is_unset(cls, value: Any) -> Any:
        # `PRISMTEX_ACCESS_TOKEN=` in a .env file must not count as a token:
        # an empty token would let any request with an empty Bearer through.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ledger_path", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, value: Any) -> Any:
        # `PRISMTEX_LEDGER_PATH=` must mean "the default", never Path("") (the
        # working directory).
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "default_model",
        "ollama_num_ctx",
        "anthropic_api_key",
        "openai_model",
        "openai_api_key",
        "openai_base_url",
        "cost_stop_usd",
        "fake_script",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        # `PRISMTEX_OPENAI_API_KEY=` in a .env file means "unset", never the empty key.
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("ollama_url", mode="before")
    @classmethod
    def _strip_url(cls, value: Any) -> Any:
        # `PRISMTEX_OLLAMA_URL=` means the default URL.
        if isinstance(value, str):
            return value.strip() or cls.model_fields["ollama_url"].default
        return value

    @field_validator("openai_preset", mode="before")
    @classmethod
    def _preset_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or None
        return value

    @field_validator("anthropic_fallbacks", mode="before")
    @classmethod
    def _fallbacks_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or "off"
        return value

    @field_validator("openai_price", mode="before")
    @classmethod
    def _parse_price(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                return None
            value = value.split(",")
        if isinstance(value, list | tuple):
            parts: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
            try:
                prices = tuple(float(part) for part in parts)
            except (TypeError, ValueError):
                prices = ()
            if len(prices) != 2 or not all(math.isfinite(p) and p >= 0 for p in prices):
                raise ValueError(
                    'must be "input,output": two USD prices per million tokens, such as "0.4,1.6"'
                )
            return prices
        return value

    @property
    def token(self) -> str | None:
        """The access token as plain text, or None when unset."""
        return self.access_token.get_secret_value() if self.access_token else None


def is_loopback_host(host: str) -> bool:
    """True for `localhost`, any 127.0.0.0/8 address and `::1`.

    The wildcard binds `0.0.0.0` and `::` are not loopback: they listen on
    every interface.
    """
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False
