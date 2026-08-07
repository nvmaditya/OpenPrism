"""Skills HTTP API: list/load/install/create/plugin scaffold."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.domain.project import ProjectStore
from app.skills.registry import SkillError, SkillRegistry

router = APIRouter(prefix="/api/skills", tags=["skills"])


class InstallBody(BaseModel):
    name: str = Field(description="Catalog skill name to install into skill-home")


class CreateSkillBody(BaseModel):
    name: str
    description: str = ""
    body: str = "# New skill\n\nDescribe the workflow here.\n"


class PluginScaffoldBody(BaseModel):
    name: str
    project_id: str | None = Field(
        default=None,
        description="If set, scaffold under that project root; else under skill-home/plugins/<name>",
    )
    description: str = ""
    version: str = "0.1.0"


def _registry(request: Request) -> SkillRegistry:
    reg = getattr(request.app.state, "skill_registry", None)
    if reg is None:
        raise HTTPException(status_code=500, detail="skill registry not configured")
    return reg  # type: ignore[no-any-return]


def _store(request: Request) -> ProjectStore:
    return request.app.state.project_store  # type: ignore[attr-defined]


def _http(exc: SkillError) -> HTTPException:
    status = 404 if exc.code == "not_found" else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.get("")
def list_skills(request: Request) -> dict[str, Any]:
    reg = _registry(request)
    skills = [s.to_dict() for s in reg.list_skills()]
    return {"skills": skills, "count": len(skills)}


@router.get("/{name}")
def load_skill(name: str, request: Request) -> dict[str, Any]:
    reg = _registry(request)
    try:
        return reg.load_skill(name)
    except SkillError as exc:
        raise _http(exc) from exc


@router.post("/install")
def install_skill(body: InstallBody, request: Request) -> dict[str, Any]:
    reg = _registry(request)
    try:
        return reg.install_from_catalog(body.name)
    except SkillError as exc:
        raise _http(exc) from exc


@router.post("/create")
def create_skill(body: CreateSkillBody, request: Request) -> dict[str, Any]:
    reg = _registry(request)
    try:
        return reg.create_skill(body.name, body.description, body.body)
    except SkillError as exc:
        raise _http(exc) from exc


@router.post("/plugin-scaffold")
def plugin_scaffold(body: PluginScaffoldBody, request: Request) -> dict[str, Any]:
    reg = _registry(request)
    store = _store(request)
    try:
        if body.project_id:
            # scaffold inside project root (allowed workspace)
            store.get(body.project_id)
            target = store.projects_root / body.project_id
            return reg.scaffold_plugin(
                target,
                name=body.name,
                version=body.version,
                description=body.description,
            )
        # default: skill-home/plugins/<name>
        target = reg.skill_home / "plugins" / body.name
        target.mkdir(parents=True, exist_ok=True)
        return reg.scaffold_plugin(
            target,
            name=body.name,
            version=body.version,
            description=body.description,
        )
    except SkillError as exc:
        raise _http(exc) from exc
