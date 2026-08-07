from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.compile import router as compile_router
from app.api.pdf import router as pdf_router
from app.api.projects import router as projects_router
from app.compile.engine import CompileEngine
from app.domain.project import ProjectStore
from app.health import health_payload
from app.previews.pdf_render import PreviewService
from app.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    projects_root = Path(settings.prism_projects_root)
    if not projects_root.is_absolute():
        projects_root = projects_root.resolve()
    projects_root.mkdir(parents=True, exist_ok=True)

    preview_root = Path(settings.prism_preview_root)
    if not preview_root.is_absolute():
        preview_root = preview_root.resolve()
    preview_root.mkdir(parents=True, exist_ok=True)

    store = ProjectStore(projects_root)
    compile_engine = CompileEngine(store)
    preview_service = PreviewService(store, preview_root)

    app = FastAPI(title="Prism API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        return health_payload()

    # compile/pdf routers first so static paths like /tools win over {project_id}
    app.include_router(compile_router)
    app.include_router(pdf_router)
    app.include_router(projects_router)

    app.state.settings = settings  # type: ignore[attr-defined]
    app.state.project_store = store  # type: ignore[attr-defined]
    app.state.compile_engine = compile_engine  # type: ignore[attr-defined]
    app.state.preview_service = preview_service  # type: ignore[attr-defined]
    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
