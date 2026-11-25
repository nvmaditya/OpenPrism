"""Composition root. There is no module-level app: call `create_app`."""

from __future__ import annotations

from fastapi import FastAPI

from prismtex import __version__
from prismtex.api import compile as compile_routes
from prismtex.api import files, health, outline, projects
from prismtex.compile import Compiler, CompileService
from prismtex.errors import install_error_handlers
from prismtex.layout import DataLayout
from prismtex.migrate import migrate_legacy_projects
from prismtex.security import SecurityMiddleware
from prismtex.settings import Settings
from prismtex.workspace import ProjectStore

MB = 2**20


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
    )
    install_error_handlers(app)
    app.add_middleware(SecurityMiddleware, settings=settings)

    app.state.settings = settings
    app.state.store = store
    app.state.compiler = (
        compiler if compiler is not None else Compiler(store, timeout_s=settings.compile_timeout_s)
    )

    for router in (
        health.router,
        projects.router,
        files.router,
        outline.router,
        compile_routes.router,
    ):
        app.include_router(router)
    return app
