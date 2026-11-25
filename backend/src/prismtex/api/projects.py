"""Projects: create from a template, import a ZIP, list, inspect, update, delete."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel, Field

from prismtex.api.deps import SettingsDep, StoreDep
from prismtex.errors import PayloadTooLarge
from prismtex.workspace import UNSET, Engine, ProjectInfo, ProjectKind, TemplateKind

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectOut(BaseModel):
    id: str
    name: str
    kind: ProjectKind
    created_at: str
    updated_at: str
    root_file: str | None
    engine: Engine


class ProjectList(BaseModel):
    projects: list[ProjectOut]


class CreateProject(BaseModel):
    kind: TemplateKind = "blank"
    name: str | None = Field(default=None, max_length=1000)


class UpdateProject(BaseModel):
    """Only the fields present change; `"root_file": null` clears the root file."""

    name: str | None = Field(default=None, max_length=1000)
    root_file: str | None = None
    engine: Engine | None = None


def _out(info: ProjectInfo) -> ProjectOut:
    return ProjectOut.model_validate(info.to_dict())


@router.get("")
def list_projects(store: StoreDep) -> ProjectList:
    return ProjectList(projects=[_out(info) for info in store.list()])


@router.post("", status_code=201)
def create_project(body: CreateProject, store: StoreDep) -> ProjectOut:
    return _out(store.create(kind=body.kind, name=body.name))


@router.post("/import", status_code=201)
def import_project(
    store: StoreDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="A ZIP archive of a LaTeX project")],
    name: Annotated[str | None, Form(max_length=1000)] = None,
) -> ProjectOut:
    stream = file.file
    size = file.size
    if size is None:
        size = stream.seek(0, 2)
    if size > store.import_max_bytes:
        raise PayloadTooLarge(settings.import_max_mb)
    stream.seek(0)
    return _out(store.import_zip(stream, name=name))


@router.get("/{project_id}")
def get_project(project_id: str, store: StoreDep) -> ProjectOut:
    return _out(store.get(project_id))


@router.patch("/{project_id}")
def update_project(project_id: str, body: UpdateProject, store: StoreDep) -> ProjectOut:
    root_file = body.root_file if "root_file" in body.model_fields_set else UNSET
    info = store.update(project_id, name=body.name, root_file=root_file, engine=body.engine)
    return _out(info)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, store: StoreDep) -> Response:
    store.delete(project_id)
    return Response(status_code=204)
