"""Routes of the retired agent, skills, previews and tool runner are gone."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/projects/{id}/chat"),
        ("POST", "/api/projects/{id}/tools/run"),
        ("GET", "/api/skills"),
        ("GET", "/api/skills/imagegen"),
        ("POST", "/api/skills/install"),
        ("POST", "/api/projects/{id}/previews/render"),
        ("GET", "/api/projects/{id}/previews/abc/page-1.png"),
        ("GET", "/health"),
    ],
)
def test_removed_routes_are_404(client: TestClient, method: str, path: str) -> None:
    pid = client.post("/api/projects", json={}).json()["id"]
    response = client.request(method, path.format(id=pid), json={"message": "hi"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_openapi_lists_no_removed_routes(client: TestClient) -> None:
    paths = client.get("/api/openapi.json").json()["paths"]
    for fragment in ("chat", "skills", "previews", "tools"):
        assert not any(fragment in path for path in paths), fragment
