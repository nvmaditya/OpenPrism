"""Shared fixtures. Tests never read a developer's `.env` or PRISMTEX_* variables."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prismtex.compile import CompileService
from prismtex.layout import DataLayout
from prismtex.main import create_app
from prismtex.settings import Settings
from prismtex.workspace import FileVersion, ProjectStore

# The Host check rejects TestClient's default "testserver".
BASE_URL = "http://127.0.0.1:8000"

MakeSettings = Callable[..., Settings]
MakeClient = Callable[..., TestClient]
Put = Callable[[ProjectStore, str, str, str], FileVersion]

# Provider keys and model endpoints never reach a test.
_STRIPPED_VARS = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"}
)
_STRIPPED_PREFIXES = ("PRISMTEX_", "OLLAMA_")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        upper = name.upper()
        if upper in _STRIPPED_VARS or upper.startswith(_STRIPPED_PREFIXES):
            monkeypatch.delenv(name)


# -- no network: tests may only open loopback connections ---------------------

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _is_loopback(host: object) -> bool:
    if host is None or host == "localhost":
        return True
    name = host.decode() if isinstance(host, bytes) else str(host)
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False  # any other host name


def _refuse(host: object) -> Any:
    raise ConnectionRefusedError(f"tests may not open non-loopback connections ({host!r})")


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    # Covers asyncio and the Windows proactor loop, which resolve through it.
    return _real_getaddrinfo(host, *args, **kwargs) if _is_loopback(host) else _refuse(host)


def _guarded_connect(sock: socket.socket, address: Any) -> None:
    if isinstance(address, tuple) and not _is_loopback(address[0]):  # pyright: ignore[reportUnknownArgumentType]
        _refuse(address[0])  # pyright: ignore[reportUnknownArgumentType]
    return _real_connect(sock, address)


def _guarded_connect_ex(sock: socket.socket, address: Any) -> int:
    if isinstance(address, tuple) and not _is_loopback(address[0]):  # pyright: ignore[reportUnknownArgumentType]
        _refuse(address[0])  # pyright: ignore[reportUnknownArgumentType]
    return _real_connect_ex(sock, address)


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every non-loopback connection; httpx.MockTransport opens no socket."""
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def blank_project(tmp_path: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(DataLayout(tmp_path))
    store.layout.ensure()
    return store, store.create(kind="blank", name="p").id


def _put(store: ProjectStore, pid: str, path: str, text: str) -> FileVersion:
    """Create or overwrite a text file the way an agent-side caller would."""
    if not any(entry.path == path for entry in store.list_files(pid)):
        return store.write_new(pid, path, text)
    return store.write(pid, path, text, base_version=store.read(pid, path).version)


@pytest.fixture
def put() -> Put:
    return _put


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def make_settings(data_dir: Path) -> MakeSettings:
    """Settings over the test's data dir; keyword arguments override fields."""

    def make(**overrides: Any) -> Settings:
        overrides.setdefault("data_dir", data_dir)
        # pyright does not see pydantic-settings' `_env_file` init option.
        return Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]

    return make


@pytest.fixture
def settings(make_settings: MakeSettings) -> Settings:
    return make_settings()


@pytest.fixture
def make_client(settings: Settings) -> Iterator[MakeClient]:
    """Build a TestClient; pass `settings=`, `compiler=` or TestClient options."""
    clients: list[TestClient] = []

    def make(
        *,
        settings: Settings = settings,
        compiler: CompileService | None = None,
        raise_server_exceptions: bool = True,
    ) -> TestClient:
        app = create_app(settings, compiler=compiler)
        client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=raise_server_exceptions)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client: MakeClient) -> TestClient:
    return make_client()


@pytest.fixture
def app(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


@pytest.fixture
def store(settings: Settings) -> ProjectStore:
    layout = DataLayout(settings.data_dir)
    layout.ensure()
    return ProjectStore(layout)
