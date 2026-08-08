"""Project HTTP API."""

from __future__ import annotations

import base64
import io

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.domain.project import ProjectError, ProjectKind, ProjectStore

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectBody(BaseModel):
    kind: ProjectKind = "blank"
    name: str | None = None


class WriteFileBody(BaseModel):
    content: str = Field(default="")


class RenameFileBody(BaseModel):
    from_path: str = Field(alias="from")
    to_path: str = Field(alias="to")

    model_config = {"populate_by_name": True}


class DeleteFilesBody(BaseModel):
    paths: list[str] = Field(min_length=1)


def _store(request: Request) -> ProjectStore:
    return request.app.state.project_store  # type: ignore[attr-defined]


def _http(exc: ProjectError) -> HTTPException:
    status = 404 if exc.code in ("not_found", "file_not_found") else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("")
def list_projects(request: Request) -> dict:
    store = _store(request)
    return {"projects": [p.to_dict() for p in store.list_projects()]}


@router.post("")
def create_project(body: CreateProjectBody, request: Request) -> dict:
    store = _store(request)
    try:
        if body.kind == "import":
            raise ProjectError(
                "use POST /api/projects/import for zip import",
                code="invalid_kind",
            )
        return store.create(kind=body.kind, name=body.name).to_dict()
    except ProjectError as exc:
        raise _http(exc) from exc


@router.post("/import")
async def import_project(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
) -> dict:
    store = _store(request)
    try:
        raw = await file.read()
        return store.import_zip(io.BytesIO(raw), name=name).to_dict()
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}")
def get_project(project_id: str, request: Request) -> dict:
    store = _store(request)
    try:
        return store.get(project_id).to_dict()
    except ProjectError as exc:
        raise _http(exc) from exc


@router.delete("/{project_id}")
def delete_project(project_id: str, request: Request) -> dict:
    store = _store(request)
    try:
        store.delete(project_id)
        return {"ok": True, "id": project_id}
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/files")
def list_files(project_id: str, request: Request) -> dict:
    store = _store(request)
    try:
        return {"files": store.list_files(project_id)}
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/tree")
def get_tree(project_id: str, request: Request, path: str = "") -> dict:
    """Structured directory tree (dirs + files), not a flat path list."""
    store = _store(request)
    try:
        return {"tree": store.tree(project_id, subpath=path)}
    except ProjectError as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/upload")
async def upload_file(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    path: str | None = Form(default=None),
) -> dict:
    """Upload a binary or text asset into the project workspace."""
    store = _store(request)
    try:
        raw = await file.read()
        filename = file.filename or "upload.bin"
        return store.upload_file(
            project_id,
            filename=filename,
            content=raw,
            dest=path,
        )
    except ProjectError as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/files/rename")
def rename_file(project_id: str, body: RenameFileBody, request: Request) -> dict:
    store = _store(request)
    try:
        result = store.rename_file(project_id, body.from_path, body.to_path)
        return {"ok": True, **result}
    except ProjectError as exc:
        raise _http(exc) from exc


@router.post("/{project_id}/files/delete")
def delete_files_batch(
    project_id: str, body: DeleteFilesBody, request: Request
) -> dict:
    """Delete one or more files (batch). Prefer this for multi-select delete."""
    store = _store(request)
    try:
        # ensure project exists
        store.get(project_id)
        return store.delete_files(project_id, body.paths)
    except ProjectError as exc:
        raise _http(exc) from exc


@router.delete("/{project_id}/files/{file_path:path}")
def delete_file(project_id: str, file_path: str, request: Request) -> dict:
    store = _store(request)
    try:
        store.delete_file(project_id, file_path)
        return {"ok": True, "path": file_path}
    except ProjectError as exc:
        raise _http(exc) from exc


@router.get("/{project_id}/files/{file_path:path}")
def read_file(project_id: str, file_path: str, request: Request) -> dict:
    store = _store(request)
    try:
        data = store.read_file(project_id, file_path)
        try:
            text = data.decode("utf-8")
            return {"path": file_path, "encoding": "utf-8", "content": text}
        except UnicodeDecodeError:
            return {
                "path": file_path,
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
            }
    except ProjectError as exc:
        raise _http(exc) from exc


@router.put("/{project_id}/files/{file_path:path}")
def write_file(
    project_id: str,
    file_path: str,
    body: WriteFileBody,
    request: Request,
) -> dict:
    store = _store(request)
    try:
        store.write_file(project_id, file_path, body.content)
        return {"ok": True, "path": file_path}
    except ProjectError as exc:
        raise _http(exc) from exc
