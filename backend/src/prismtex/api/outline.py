"""The project outline: headings with file and 1-based line, for jump-to-heading."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from prismtex.api.deps import StoreDep
from prismtex.outline import outline_project

router = APIRouter(prefix="/api/projects/{project_id}", tags=["outline"])


class OutlineItem(BaseModel):
    title: str
    line: int
    level: int
    macro: str
    path: str


class Outline(BaseModel):
    outline: list[OutlineItem]


@router.get("/outline")
def get_outline(project_id: str, store: StoreDep) -> Outline:
    items = outline_project(store, project_id)
    return Outline(outline=[OutlineItem.model_validate(item) for item in items])
