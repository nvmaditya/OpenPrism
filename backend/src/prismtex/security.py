"""Host, access-token and Origin checks for every HTTP request (ADR 0005).

A pure ASGI middleware, so streaming responses pass through untouched.
The checks run in a fixed order: Host (blocks DNS rebinding), then the
access token (only when one is configured), then Origin on state-changing
methods (blocks cross-site requests from pages the user visits).
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from prismtex.errors import error_response
from prismtex.settings import Settings, is_loopback_host

LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TOKEN_COOKIE = "prismtex_token"
HEALTH_PATH = "/api/health"


def host_header_name(value: str) -> str | None:
    """The lowercased hostname of a Host header value, or None if malformed."""
    value = value.strip().lower()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        name, rest = value[1:end], value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return name or None
    if value.count(":") > 1:
        return None  # a bare IPv6 address is not a valid Host header
    name, sep, port = value.partition(":")
    if sep and not port.isdigit():
        return None
    return name or None


def _origin_key(origin: str) -> tuple[str, str] | None:
    """(scheme, hostname) of an Origin header value, or None if malformed."""
    try:
        parts = urlsplit(origin)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not hostname:
        return None
    return parts.scheme, hostname


def _cookie_value(header: str, name: str) -> str | None:
    for pair in header.split(";"):
        key, sep, value = pair.strip().partition("=")
        if sep and key == name:
            return value.strip().strip('"')
    return None


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.allowed_hosts = LOOPBACK_HOSTNAMES | frozenset(settings.allowed_hosts)
        self.allowed_origins = frozenset(settings.allowed_origins)
        token = settings.token
        self.token = token.encode() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        rejection = (
            self._check_host(headers)
            or self._check_token(scope, headers)
            or self._check_origin(scope, headers)
        )
        if rejection is None:
            await self.app(scope, receive, send)
            return
        status, code, message = rejection
        extra_headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        response = error_response(status, code, message, headers=extra_headers)
        await response(scope, receive, send)

    def _check_host(self, headers: dict[str, list[str]]) -> tuple[int, str, str] | None:
        values = headers.get("host", [])
        name = host_header_name(values[0]) if len(values) == 1 else None
        if name is None or name not in self.allowed_hosts:
            return (
                400,
                "host_not_allowed",
                "This server does not answer to that host name. "
                "Add it to PRISMTEX_ALLOWED_HOSTS if you trust it.",
            )
        return None

    def _check_token(
        self, scope: Scope, headers: dict[str, list[str]]
    ) -> tuple[int, str, str] | None:
        if self.token is None:
            return None
        if scope["path"] == HEALTH_PATH and scope["method"] in ("GET", "HEAD"):
            return None
        presented: list[str] = []
        for value in headers.get("authorization", []):
            scheme, _, credentials = value.strip().partition(" ")
            if scheme.lower() == "bearer":
                presented.append(credentials.strip())
        for value in headers.get("cookie", []):
            cookie = _cookie_value(value, TOKEN_COOKIE)
            if cookie is not None:
                presented.append(cookie)
        # Compare every candidate so timing does not reveal which one matched.
        ok = False
        for candidate in presented:
            ok |= hmac.compare_digest(candidate.encode(), self.token)
        if not ok:
            return (401, "unauthorized", "This server needs an access token.")
        return None

    def _check_origin(
        self, scope: Scope, headers: dict[str, list[str]]
    ) -> tuple[int, str, str] | None:
        if scope["method"] in SAFE_METHODS:
            return None
        origins = headers.get("origin", [])
        if not origins:
            fetch_site = headers.get("sec-fetch-site", [""])[0].strip().lower()
            if fetch_site == "cross-site":
                return _origin_rejection()
            return None  # a non-browser client
        if len(origins) > 1:
            return _origin_rejection()
        origin = origins[0].strip().lower().rstrip("/")
        if origin == "null":
            # Sandboxed frames and file:// pages send "null"; never trust it.
            return _origin_rejection()
        if origin in self.allowed_origins:
            return None
        key = _origin_key(origin)
        if key is not None and is_loopback_host(key[1]):
            return None
        return _origin_rejection()


def _origin_rejection() -> tuple[int, str, str]:
    return (
        403,
        "origin_not_allowed",
        "Requests from this origin are not allowed. "
        "Add it to PRISMTEX_ALLOWED_ORIGINS if you trust it.",
    )


def _headers(scope: Scope) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw_name, raw_value in scope["headers"]:
        name = raw_name.decode("latin-1").lower()
        out.setdefault(name, []).append(raw_value.decode("latin-1"))
    return out
