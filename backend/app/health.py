"""Health check — pure function, easy to unit-test without ASGI."""

from datetime import datetime, timezone


def health_payload() -> dict:
    return {
        "status": "ok",
        "service": "prism-backend",
        "version": "0.1.0",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
