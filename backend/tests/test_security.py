"""Host, Origin and access-token checks (ADR 0005)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from prismtex.security import host_header_name
from prismtex.settings import Settings

MakeClient = Callable[..., TestClient]
MakeSettings = Callable[..., Settings]


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("localhost", "localhost"),
        ("localhost:3000", "localhost"),
        ("LOCALHOST:8000", "localhost"),
        ("127.0.0.1:8000", "127.0.0.1"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("evil.example", "evil.example"),
        ("", None),
        ("::1", None),
        ("[::1", None),
        ("[::1]x", None),
        ("localhost:http", None),
    ],
)
def test_host_header_name(value: str, name: str | None) -> None:
    assert host_header_name(value) == name


# -- Host ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:3000", "127.0.0.1:8000", "[::1]:8000", "LocalHost:1"]
)
def test_loopback_hosts_are_accepted(client: TestClient, host: str) -> None:
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "127.0.0.2", "0.0.0.0", "localhost.evil.example"]
)
def test_other_hosts_are_rejected(client: TestClient, host: str) -> None:
    response = client.get("/api/health", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "host_not_allowed"


def test_host_check_covers_every_route(client: TestClient) -> None:
    response = client.post("/api/projects", json={}, headers={"Host": "evil.example"})
    assert response.status_code == 400
    assert client.get("/api/projects").json() == {"projects": []}


def test_allowed_hosts_setting(make_client: MakeClient, make_settings: MakeSettings) -> None:
    client = make_client(settings=make_settings(allowed_hosts=("Box.LAN",)))
    assert client.get("/api/health", headers={"Host": "box.lan:8000"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "other.lan"}).status_code == 400


# -- Origin ----------------------------------------------------------------------


def create(client: TestClient, headers: dict[str, str]) -> int:
    return client.post("/api/projects", json={}, headers=headers).status_code


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000", "https://localhost"],
)
def test_loopback_origins_are_accepted(client: TestClient, origin: str) -> None:
    assert create(client, {"Origin": origin}) == 201


def test_missing_origin_is_accepted(client: TestClient) -> None:
    assert create(client, {}) == 201
    assert create(client, {"Sec-Fetch-Site": "same-origin"}) == 201


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "http://localhost.evil.example"},
        {"Origin": "null"},
        {"Origin": "file://"},
        {"Origin": "not a url"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_foreign_origins_are_rejected(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/projects", json={}, headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_not_allowed"
    assert client.get("/api/projects").json() == {"projects": []}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_origin_check_covers_state_changing_methods(client: TestClient, method: str) -> None:
    pid = client.post("/api/projects", json={}).json()["id"]
    response = client.request(
        method,
        f"/api/projects/{pid}" if method != "PUT" else f"/api/projects/{pid}/files/main.tex",
        json={"content": "x"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_safe_methods_skip_the_origin_check(client: TestClient) -> None:
    response = client.get("/api/projects", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_allowed_origins_setting(make_client: MakeClient, make_settings: MakeSettings) -> None:
    client = make_client(settings=make_settings(allowed_origins=("https://ui.example",)))
    assert create(client, {"Origin": "https://ui.example"}) == 201
    assert create(client, {"Origin": "https://other.example"}) == 403


# -- access token --------------------------------------------------------------------


@pytest.fixture
def token_client(make_client: MakeClient, make_settings: MakeSettings) -> TestClient:
    return make_client(settings=make_settings(access_token="s3cret-token"))


def test_token_is_required(token_client: TestClient) -> None:
    response = token_client.get("/api/projects")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"
    wrong = token_client.get("/api/projects", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert token_client.get("/api/openapi.json").status_code == 401


def test_bearer_header(token_client: TestClient) -> None:
    ok = token_client.get("/api/projects", headers={"Authorization": "Bearer s3cret-token"})
    assert ok.status_code == 200
    lower = token_client.get("/api/projects", headers={"Authorization": "bearer s3cret-token"})
    assert lower.status_code == 200


def test_cookie(token_client: TestClient) -> None:
    response = token_client.get(
        "/api/projects", headers={"Cookie": "other=1; prismtex_token=s3cret-token"}
    )
    assert response.status_code == 200
    wrong = token_client.get("/api/projects", headers={"Cookie": "prismtex_token=nope"})
    assert wrong.status_code == 401


def test_health_is_exempt(token_client: TestClient) -> None:
    assert token_client.get("/api/health").status_code == 200
    assert token_client.post("/api/health").status_code == 401


def test_host_is_checked_before_the_token(token_client: TestClient) -> None:
    response = token_client.get("/api/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_token_is_checked_before_origin(token_client: TestClient) -> None:
    response = token_client.post(
        "/api/projects", json={}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 401
    authorized = token_client.post(
        "/api/projects",
        json={},
        headers={"Origin": "https://evil.example", "Authorization": "Bearer s3cret-token"},
    )
    assert authorized.status_code == 403
