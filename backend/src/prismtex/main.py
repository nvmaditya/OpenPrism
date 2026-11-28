"""Composition root. There is no module-level app: call `create_app`."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from prismtex import __version__
from prismtex.api import compile as compile_routes
from prismtex.api import files, health, outline, projects
from prismtex.compile import Compiler, CompileService
from prismtex.compile.tree import ScratchTreeCompiler
from prismtex.errors import PayloadTooLarge, error_response, install_error_handlers
from prismtex.layout import DataLayout
from prismtex.migrate import migrate_legacy_projects
from prismtex.security import SecurityMiddleware
from prismtex.settings import Settings
from prismtex.workspace import ProjectStore, remove_stale_temp_files

MB = 2**20

# A generous ceiling for JSON routes: a 10 MB LaTeX file can become ~20 MB of
# JSON once every backslash and control character is escaped, so the cap must
# sit well above the text-size limit, not next to it.
JSON_BODY_CAP = 64 * MB


def create_app(
    settings: Settings | None = None, *, compiler: CompileService | None = None
) -> FastAPI:
    """Build the app over `settings.data_dir`.

    `compiler` is a test seam: a fake with the compiler's interface.
    """
    settings = settings or Settings()
    layout = DataLayout(settings.data_dir)
    layout.ensure()
    migrate_legacy_projects(layout)
    remove_stale_temp_files(layout)
    store = ProjectStore(
        layout,
        max_upload_bytes=settings.max_upload_mb * MB,
        import_max_bytes=settings.import_max_mb * MB,
        import_max_files=settings.import_max_files,
    )

    app = FastAPI(
        title="PrismTeX",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        # Spec 12: every route lives under /api. The Swagger OAuth2 redirect
        # default sits at /docs/..., which the UI proxy never forwards; there
        # is no OAuth2 scheme, so drop the route rather than relocate it.
        swagger_ui_oauth2_redirect_url=None,
    )
    install_error_handlers(app)
    # Added before SecurityMiddleware so it becomes the inner layer: the Host,
    # token and Origin checks run first and reject cross-site requests before a
    # body is ever read.
    app.add_middleware(BodyLimitMiddleware, settings=settings)
    app.add_middleware(SecurityMiddleware, settings=settings)

    app.state.settings = settings
    app.state.store = store
    service: CompileService = (
        compiler if compiler is not None else Compiler(store, timeout_s=settings.compile_timeout_s)
    )
    app.state.compiler = service
    # Every compile, the user's included, goes through the tree compiler and
    # its cache; leftover Turn scratch folders are removed at startup.
    tree_compiler = ScratchTreeCompiler(service, layout)
    tree_compiler.sweep()
    app.state.tree_compiler = tree_compiler

    for router in (
        health.router,
        projects.router,
        files.router,
        outline.router,
        compile_routes.router,
    ):
        app.include_router(router)
    return app


_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class BodyLimitMiddleware:
    """Refuse an over-large request body before the route reads it (ASGI).

    Starlette spools a multipart part and buffers a JSON body in full before a
    route's own size check runs, so a single request can spend hundreds of MB.
    This caps each route up front: by Content-Length when the client declares
    one, otherwise by counting the bytes that stream in.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.import_cap = (settings.import_max_mb + 1) * MB
        self.upload_cap = (settings.max_upload_mb + 1) * MB

    def _cap(self, path: str) -> int:
        if path == "/api/projects/import":
            return self.import_cap
        if path.endswith("/upload"):
            return self.upload_cap
        return JSON_BODY_CAP

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return
        cap = self._cap(scope["path"])
        declared = _content_length(scope)
        if declared is not None and declared > cap:
            await _too_large(cap)(scope, receive, send)
            return
        if declared is not None:
            # The declared length is within the cap; trust it and stream normally.
            await self.app(scope, receive, send)
            return
        await self.app(scope, _capped_receive(receive, cap), send)


def _capped_receive(receive: Receive, cap: int) -> Receive:
    """Wrap `receive` to count a body with no Content-Length and stop it at `cap`.

    Exceeding the cap raises PayloadTooLarge; the raise happens while the route
    is still reading its body, so the error handler turns it into a 413.
    """
    seen = 0

    async def wrapped() -> Any:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > cap:
                raise PayloadTooLarge(cap / MB)
        return message

    return wrapped


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _too_large(cap: int) -> ASGIApp:
    cap_mb = cap // MB
    return error_response(
        413,
        "payload_too_large",
        f"The request body is larger than the {cap_mb} MB limit for this route.",
        limit_mb=cap_mb,
    )
