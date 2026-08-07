from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.projects import router as projects_router
from app.domain.project import ProjectStore
from app.health import health_payload
from app.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    projects_root = Path(settings.prism_projects_root)
    if not projects_root.is_absolute():
        projects_root = projects_root.resolve()
    projects_root.mkdir(parents=True, exist_ok=True)

    store = ProjectStore(projects_root)

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

    app.include_router(projects_router)

    app.state.settings = settings  # type: ignore[attr-defined]
    app.state.project_store = store  # type: ignore[attr-defined]
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
