"""FastAPI dependencies: the objects `create_app` put on `app.state`."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from prismtex.compile import CompileService
from prismtex.compile.tree import TreeCompiler
from prismtex.history import History
from prismtex.settings import Settings
from prismtex.workspace import ProjectStore


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> ProjectStore:
    return request.app.state.store


def get_compiler(request: Request) -> CompileService:
    return request.app.state.compiler


def get_tree_compiler(request: Request) -> TreeCompiler:
    return request.app.state.tree_compiler


def get_history(request: Request) -> History:
    return request.app.state.history


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[ProjectStore, Depends(get_store)]
CompilerDep = Annotated[CompileService, Depends(get_compiler)]
TreeCompilerDep = Annotated[TreeCompiler, Depends(get_tree_compiler)]
HistoryDep = Annotated[History, Depends(get_history)]
