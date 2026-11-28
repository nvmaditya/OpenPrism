"""FastAPI dependencies: the objects `create_app` put on `app.state`."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from prismtex.compile import CompileService
from prismtex.settings import Settings
from prismtex.workspace import ProjectStore


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> ProjectStore:
    return request.app.state.store


def get_compiler(request: Request) -> CompileService:
    return request.app.state.compiler


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[ProjectStore, Depends(get_store)]
CompilerDep = Annotated[CompileService, Depends(get_compiler)]
