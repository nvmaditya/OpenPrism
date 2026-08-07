"""Compile and TeX tool HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.compile.engine import CompileEngine
from app.domain.project import ProjectError, ProjectStore
from app.sandbox.process import TEX_ALLOWLIST

router = APIRouter(prefix="/api/projects", tags=["compile"])


class CompileBody(BaseModel):
    engine: Literal["latexmk", "pdflatex", "xelatex", "lualatex", "latex"] = "latexmk"
    main_file: str = "main.tex"
    timeout_s: float = Field(default=180.0, ge=5.0, le=600.0)


class ToolRunBody(BaseModel):
    tool: str
    args: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=120.0, ge=1.0, le=600.0)


def _store(request: Request) -> ProjectStore:
    return request.app.state.project_store  # type: ignore[attr-defined]


def _engine(request: Request) -> CompileEngine:
    store = _store(request)
    cached = getattr(request.app.state, "compile_engine", None)
    if cached is None:
        cached = CompileEngine(store)
        request.app.state.compile_engine = cached  # type: ignore[attr-defined]
    return cached  # type: ignore[no-any-return]


def _http(exc: ProjectError) -> HTTPException:
    status = 404 if exc.code in ("not_found", "file_not_found") else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("/tools")
def list_tools() -> dict[str, Any]:
    return {"tools": sorted(TEX_ALLOWLIST)}


@router.post("/{project_id}/compile")
def compile_project(
    project_id: str,
    body: CompileBody,
    request: Request,
) -> dict[str, Any]:
    eng = _engine(request)
    try:
        result = eng.compile(
            project_id,
            engine=body.engine,
            main_file=body.main_file,
            timeout_s=body.timeout_s,
        )
        return result.to_dict()
    except ProjectError as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/tools/run")
def run_tool(
    project_id: str,
    body: ToolRunBody,
    request: Request,
) -> dict[str, Any]:
    eng = _engine(request)
    try:
        return eng.run_tool(
            project_id,
            body.tool,
            body.args,
            timeout_s=body.timeout_s,
        )
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/logs")
def list_logs(project_id: str, request: Request) -> dict[str, Any]:
    eng = _engine(request)
    try:
        return {"logs": eng.list_logs(project_id)}
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/logs/{log_id}")
def get_log(project_id: str, log_id: str, request: Request) -> dict[str, Any]:
    eng = _engine(request)
    try:
        return eng.get_log(project_id, log_id)
    except ProjectError as exc:
        raise _http(exc) from exc
