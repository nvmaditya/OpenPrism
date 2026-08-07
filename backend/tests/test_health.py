from fastapi.testclient import TestClient

from app.health import health_payload
from app.main import create_app


def test_health_payload_shape():
    body = health_payload()
    assert body["status"] == "ok"
    assert body["service"] == "prism-backend"
    assert "version" in body
    assert "ts" in body


def test_health_endpoint_via_asgi():
    client = TestClient(create_app())
    r1 = client.get("/health")
    r2 = client.get("/health")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["status"] == "ok"
    assert r2.json()["status"] == "ok"
    assert r1.json()["service"] == "prism-backend"
