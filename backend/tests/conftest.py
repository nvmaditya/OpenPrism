"""Shared fixtures. Tests never read a developer's `.env` or PRISMTEX_* variables."""

from __future__ import annotations

import os
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
from prismtex.workspace import ProjectStore

# The Host check rejects TestClient's default "testserver".
BASE_URL = "http://127.0.0.1:8000"

MakeSettings = Callable[..., Settings]
MakeClient = Callable[..., TestClient]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("PRISMTEX_"):
            monkeypatch.delenv(name)


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
