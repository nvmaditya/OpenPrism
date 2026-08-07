"""Project-scoped FS tools wrapping real ProjectStore (path sandbox included)."""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.domain.project import ProjectError, ProjectStore


class PathArgs(BaseModel):
    path: str = Field(description="Relative path inside the project workspace")


class WriteArgs(BaseModel):
    path: str = Field(description="Relative path inside the project workspace")
    content: str = Field(description="Full text content to write")


def make_fs_tools(store: ProjectStore, project_id: str) -> list[StructuredTool]:
    """Build LangChain tools bound to one project id + ProjectStore."""

    def read_project_file(path: str) -> str:
        """Read a UTF-8 text file from the current project (relative path only)."""
        try:
            data = store.read_file(project_id, path)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return f"<binary {len(data)} bytes>"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def write_project_file(path: str, content: str) -> str:
        """Write text to a relative path in the current project workspace."""
        try:
            store.write_file(project_id, path, content)
            return f"OK wrote {path} ({len(content)} chars)"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def list_project_files() -> str:
        """List relative file paths in the current project workspace."""
        try:
            files = store.list_files(project_id)
            return "\n".join(files) if files else "(empty)"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    return [
        StructuredTool.from_function(
            func=read_project_file,
            name="read_project_file",
            description=(
                "Read a UTF-8 text file from the project workspace. "
                "Path must be relative (no .. or absolute paths)."
            ),
            args_schema=PathArgs,
        ),
        StructuredTool.from_function(
            func=write_project_file,
            name="write_project_file",
            description=(
                "Write text content to a relative path in the project workspace. "
                "Path must be relative (no .. or absolute paths)."
            ),
            args_schema=WriteArgs,
        ),
        StructuredTool.from_function(
            func=list_project_files,
            name="list_project_files",
            description="List all relative file paths in the project workspace.",
        ),
    ]
