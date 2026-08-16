"""Project-scoped streaming chat (SSE) over the LangGraph agent."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.stream import iter_sse_bytes
from app.compile.engine import CompileEngine
from app.domain.project import ProjectError, ProjectStore
from app.settings import Settings

router = APIRouter(prefix="/api/projects", tags=["chat"])


class ChatContext(BaseModel):
    """Optional editor context so the agent knows what the user is looking at."""

    open_path: str | None = None
    open_content: str | None = Field(
        default=None,
        description="Current editor buffer (may be truncated by client)",
    )


class ChatBody(BaseModel):
    message: str = Field(min_length=1, description="User message for the agent")
    context: ChatContext | None = None


def _store(request: Request) -> ProjectStore:
    return request.app.state.project_store  # type: ignore[attr-defined]


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[attr-defined]


def _model_override(request: Request) -> Any:
    return getattr(request.app.state, "chat_model_override", None)


def _http(exc: ProjectError) -> HTTPException:
    status = 404 if exc.code in ("not_found", "file_not_found") else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.post("/{project_id}/chat")
def chat_stream(
    project_id: str,
    body: ChatBody,
    request: Request,
) -> StreamingResponse:
    """
    SSE stream for one agent turn.

    Events: ``token``, ``tool_start``, ``tool_end``, ``file_changed``,
    ``error``, ``done``.
    """
    store = _store(request)
    try:
        store.get(project_id)
    except ProjectError as exc:
        raise _http(exc) from exc

    model = _model_override(request)
    settings = _settings(request)
    compile_engine = getattr(request.app.state, "compile_engine", None)
    if compile_engine is None:
        compile_engine = CompileEngine(store)
    open_path = body.context.open_path if body.context else None
    open_preview = body.context.open_content if body.context else None
    if open_preview and len(open_preview) > 6000:
        open_preview = open_preview[:6000]

    def generate():
        yield from iter_sse_bytes(
            store,
            project_id,
            body.message,
            model=model,
            settings=settings,
            open_path=open_path,
            open_preview=open_preview,
            compile_engine=compile_engine,
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
