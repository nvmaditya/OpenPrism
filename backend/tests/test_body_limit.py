"""The ASGI body-size guard caps a request body before a route reads it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prismtex.errors import PayloadTooLarge
from prismtex.main import JSON_BODY_CAP, MB, BodyLimitMiddleware
from prismtex.settings import Settings

MakeClient = Callable[..., TestClient]
MakeSettings = Callable[..., Settings]


def test_forged_content_length_over_cap_is_413_before_the_route(client: TestClient) -> None:
    # A huge declared Content-Length with a tiny body: rejected without the body
    # being read and without the route creating anything.
    response = client.post(
        "/api/projects",
        content=b"{}",
        headers={"content-length": str(JSON_BODY_CAP + MB), "content-type": "application/json"},
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "payload_too_large"
    assert detail["limit_mb"] == JSON_BODY_CAP // MB
    assert "for this route" in detail["message"]  # the guard, not the route's own check
    assert client.get("/api/projects").json() == {"projects": []}


def test_upload_cap_is_lower_than_the_json_cap(
    make_client: MakeClient, make_settings: MakeSettings
) -> None:
    client = make_client(settings=make_settings(max_upload_mb=1))
    pid = client.post("/api/projects", json={}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("a.png", b"x", "image/png")},
        headers={"content-length": str(5 * MB)},
    )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["limit_mb"] == 2  # max_upload_mb + 1
    assert "for this route" in detail["message"]


def test_a_ten_megabyte_all_backslash_text_put_still_succeeds(
    client: TestClient,
) -> None:
    # Every backslash doubles in JSON, so this 9 MB body is ~18 MB on the wire,
    # well past any naive ~11 MB cap, yet under the 64 MB JSON cap.
    pid = client.post("/api/projects", json={}).json()["id"]
    content = "\\" * (9 * MB)
    response = client.put(f"/api/projects/{pid}/files/big.tex", json={"content": content})
    assert response.status_code == 200
    assert client.get(f"/api/projects/{pid}/files/big.tex").json()["content"] == content


# -- the streamed path (no Content-Length) via the middleware directly --------


def _drive(middleware: BodyLimitMiddleware, scope: dict[str, Any], chunks: list[bytes]) -> Any:
    queue = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent: list[dict[str, Any]] = []
    reached = {"v": False}

    async def receive() -> Any:
        return queue.pop(0) if queue else {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        sent.append(message)

    async def app(scope: Any, recv: Any, snd: Any) -> None:
        reached["v"] = True
        while True:
            message = await recv()
            if not message.get("more_body", False):
                break

    middleware.app = app  # type: ignore[assignment]
    asyncio.run(middleware(scope, receive, send))
    return sent, reached


def test_streamed_body_without_content_length_is_capped(settings: Settings) -> None:
    middleware = BodyLimitMiddleware(_unused_app, settings=settings)
    scope = {"type": "http", "method": "POST", "path": "/api/projects", "headers": []}
    # Just under the cap streams through to the route.
    chunk = b"a" * MB
    under = [chunk] * (JSON_BODY_CAP // MB)
    _sent, reached = _drive(middleware, scope, under)
    assert reached["v"] is True
    # Over the cap raises PayloadTooLarge while the route is still reading.
    over = [chunk] * (JSON_BODY_CAP // MB + 1)
    with pytest.raises(PayloadTooLarge):
        _drive(middleware, scope, over)


async def _unused_app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
    raise AssertionError("replaced per test")
