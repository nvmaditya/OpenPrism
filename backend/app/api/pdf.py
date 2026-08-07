"""PDF serve + page preview HTTP API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.domain.project import ProjectError, ProjectStore
from app.previews.pdf_render import PreviewService

router = APIRouter(prefix="/api/projects", tags=["pdf"])


class RenderBody(BaseModel):
    pdf_path: str = "main.pdf"
    dpi: int = Field(default=72, ge=36, le=200)
    max_pages: int = Field(default=20, ge=1, le=100)


def _store(request: Request) -> ProjectStore:
    return request.app.state.project_store  # type: ignore[attr-defined]


def _previews(request: Request) -> PreviewService:
    svc = getattr(request.app.state, "preview_service", None)
    if svc is None:
        raise HTTPException(status_code=500, detail="preview service not configured")
    return svc  # type: ignore[no-any-return]


def _http(exc: ProjectError) -> HTTPException:
    status = 404 if exc.code in ("not_found", "file_not_found") else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/{project_id}/pdf")
def get_pdf(
    project_id: str,
    request: Request,
    path: str = Query(default="main.pdf", description="Relative PDF path in project"),
) -> Response:
    """Serve the compiled PDF from the project workspace (sandbox path)."""
    store = _store(request)
    try:
        pdf_file = store.resolve_file(project_id, path)
        if not pdf_file.is_file():
            raise ProjectError(f"pdf not found: {path}", code="file_not_found")
        if pdf_file.suffix.lower() != ".pdf":
            raise ProjectError("not a pdf file", code="invalid_pdf")
        data = pdf_file.read_bytes()
        if not data.startswith(b"%PDF"):
            raise ProjectError("file is not a valid PDF", code="invalid_pdf")
        return Response(
            content=data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{Path(path).name}"',
                "X-Prism-Pdf-Path": path,
            },
        )
    except ProjectError as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/previews/render")
def render_previews(
    project_id: str,
    body: RenderBody,
    request: Request,
) -> dict[str, Any]:
    """Rasterize PDF pages into the isolated preview root (not the project tree)."""
    svc = _previews(request)
    try:
        return svc.render_pdf_pages(
            project_id,
            body.pdf_path,
            dpi=body.dpi,
            max_pages=body.max_pages,
        )
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/previews/{run_id}")
def list_preview_pages(
    project_id: str,
    run_id: str,
    request: Request,
) -> dict[str, Any]:
    svc = _previews(request)
    try:
        pages = svc.list_run_pages(project_id, run_id)
        return {
            "project_id": project_id,
            "run_id": run_id,
            "pages": pages,
            "page_urls": [
                f"/api/projects/{project_id}/previews/{run_id}/{name}" for name in pages
            ],
        }
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/previews/{run_id}/{filename}")
def get_preview_page(
    project_id: str,
    run_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    svc = _previews(request)
    try:
        # only page-N.png style names
        if not filename.lower().endswith(".png"):
            raise ProjectError("only png preview pages are served", code="invalid_page")
        path = svc.resolve_preview_file(project_id, f"{run_id}/{filename}")
        if not path.is_file():
            raise ProjectError(f"preview not found: {filename}", code="not_found")
        return FileResponse(path, media_type="image/png", filename=filename)
    except ProjectError as exc:
        raise _http(exc) from exc
