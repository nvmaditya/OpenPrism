"""Server configuration: every `PRISMTEX_*` variable is a field of `Settings`."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the process environment and `.env` in the working directory."""

    model_config = SettingsConfigDict(
        env_prefix="PRISMTEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
