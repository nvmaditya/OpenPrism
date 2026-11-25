"""Compile a project, serve its PDF and read its compile logs."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from prismtex.api.deps import CompilerDep, StoreDep
from prismtex.compile import CompileResult, CompileStatus, Severity, pdf_file
from prismtex.workspace import Engine, ProjectStore

router = APIRouter(prefix="/api/projects/{project_id}", tags=["compile"])


class CompileBody(BaseModel):
    """Overrides for this run only; the project settings are not changed."""

    root_file: str | None = Field(default=None, max_length=1000)
    engine: Engine | None = None


class DiagnosticOut(BaseModel):
    file: str | None
    line: int | None
    severity: Severity
    kind: str
    message: str


class CompileOut(BaseModel):
    status: CompileStatus
    ok: bool
    diagnostics: list[DiagnosticOut]
    duration_ms: int
    engine: Engine
    root_file: str
    log_id: str | None
    pdf_url: str | None


class LogMeta(BaseModel):
    log_id: str
    created_at: str
    status: CompileStatus
    engine: Engine
    root_file: str
    duration_ms: int
    errors: int
    warnings: int


class LogList(BaseModel):
    logs: list[LogMeta]


class LogOut(LogMeta):
    log: str


def _pdf_url(store: ProjectStore, project_id: str, result: CompileResult) -> str | None:
    if result.pdf_path is None:
        return None
    try:
        mtime = result.pdf_path.stat().st_mtime_ns
    except OSError:
        return None
    # The PDF's mtime alone can repeat on a coarse-mtime file system; the
    # log_id is unique per compile, so the viewer always reloads a new PDF.
    version = f"{mtime}-{result.log_id}" if result.log_id else str(mtime)
    query: dict[str, str | int] = {"v": version}
    # GET /pdf serves the project's root file by default; a run with another
    # root file has to name it.
    if result.root_file != store.settings(project_id).root_file:
        query["root_file"] = result.root_file
    return f"/api/projects/{project_id}/pdf?{urlencode(query)}"


@router.post("/compile")
def compile_project(
    project_id: str,
    compiler: CompilerDep,
    store: StoreDep,
    body: CompileBody | None = None,
) -> CompileOut:
    # A plain `def` route: FastAPI runs the blocking compile in a worker thread.
    options = body or CompileBody()
    result = compiler.compile(project_id, root_file=options.root_file, engine=options.engine)
    return CompileOut(
        status=result.status,
        ok=result.ok,
        diagnostics=[DiagnosticOut.model_validate(d.to_dict()) for d in result.diagnostics],
        duration_ms=result.duration_ms,
        engine=result.engine,
        root_file=result.root_file,
        log_id=result.log_id,
        pdf_url=_pdf_url(store, project_id, result),
    )


@router.get(
    "/pdf",
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}, "description": "The compiled PDF"}},
)
def get_pdf(project_id: str, store: StoreDep, root_file: str | None = None) -> FileResponse:
    path = pdf_file(store, project_id, root_file)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/logs")
def list_logs(project_id: str, compiler: CompilerDep) -> LogList:
    return LogList(logs=[LogMeta.model_validate(meta) for meta in compiler.logs(project_id)])


@router.get("/logs/{log_id}")
def get_log(project_id: str, log_id: str, compiler: CompilerDep) -> LogOut:
    return LogOut.model_validate(compiler.get_log(project_id, log_id))
