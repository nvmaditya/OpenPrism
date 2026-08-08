"""Project-scoped FS tools wrapping real ProjectStore (path sandbox included)."""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.domain.project import ProjectError, ProjectStore


class PathArgs(BaseModel):
    path: str = Field(
        description="Relative path inside the project (e.g. main.tex, chapters/intro.tex)"
    )


class WriteArgs(BaseModel):
    path: str = Field(
        description="Relative path to create or overwrite (e.g. notes.tex, figures/fig1.tex)"
    )
    content: str = Field(
        description="Full file contents to write (complete LaTeX source when writing .tex files)"
    )


class EditArgs(BaseModel):
    path: str = Field(description="Relative path of an existing file to edit")
    old_str: str = Field(
        description="Exact text to find in the file (must match uniquely)"
    )
    new_str: str = Field(
        description="Replacement text (use empty string to delete the matched span)"
    )


def make_fs_tools(store: ProjectStore, project_id: str) -> list[StructuredTool]:
    """Build LangChain tools bound to one project id + ProjectStore."""

    def list_project_files() -> str:
        """List relative file paths in the project workspace."""
        try:
            files = store.list_files(project_id)
            if not files:
                return "(empty project)"
            return "Project files:\n" + "\n".join(f"- {p}" for p in files)
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def read_project_file(path: str) -> str:
        """Read a UTF-8 text file from the project (relative path only)."""
        try:
            data = store.read_file(project_id, path)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return f"<binary {len(data)} bytes at {path}>"
            # Cap huge logs but keep full .tex
            if path.endswith((".log", ".aux")) and len(text) > 8000:
                return text[:8000] + "\n… [truncated]"
            return text
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def write_project_file(path: str, content: str) -> str:
        """
        Create or overwrite a file with full contents.
        Prefer this for NEW files or full rewrites. For small edits use edit_project_file.
        """
        try:
            # Normalize newlines for Windows TeX tools
            normalized = content.replace("\r\n", "\n")
            store.write_file(project_id, path, normalized)
            return f"OK wrote {path} ({len(normalized)} chars)"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def edit_project_file(path: str, old_str: str, new_str: str) -> str:
        """
        Surgically edit an existing file by replacing one exact occurrence of old_str
        with new_str. Prefer this when changing part of main.tex or other large files.
        """
        try:
            raw = store.read_file(project_id, path)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"ERROR [invalid]: {path} is not UTF-8 text"
            if old_str == "":
                return "ERROR [invalid]: old_str must not be empty"
            count = text.count(old_str)
            if count == 0:
                # try normalized newlines
                text_n = text.replace("\r\n", "\n")
                old_n = old_str.replace("\r\n", "\n")
                count = text_n.count(old_n)
                if count == 0:
                    snippet = text[:400].replace("\n", "\\n")
                    return (
                        f"ERROR [not_found]: old_str not found in {path}. "
                        f"Read the file first. Start of file: {snippet!r}"
                    )
                if count > 1:
                    return (
                        f"ERROR [ambiguous]: old_str matches {count} times in {path}. "
                        "Include more surrounding context so it matches once."
                    )
                text = text_n.replace(old_n, new_str.replace("\r\n", "\n"), 1)
            elif count > 1:
                return (
                    f"ERROR [ambiguous]: old_str matches {count} times in {path}. "
                    "Include more surrounding context so it matches once."
                )
            else:
                text = text.replace(old_str, new_str, 1)
            store.write_file(project_id, path, text.replace("\r\n", "\n"))
            return f"OK edited {path} (1 replacement)"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    return [
        StructuredTool.from_function(
            func=list_project_files,
            name="list_project_files",
            description=(
                "List all files in the project. Call this before editing if you are "
                "unsure which .tex files exist."
            ),
        ),
        StructuredTool.from_function(
            func=read_project_file,
            name="read_project_file",
            description=(
                "Read a project file (use before edit_project_file). "
                "Path is relative, e.g. main.tex."
            ),
            args_schema=PathArgs,
        ),
        StructuredTool.from_function(
            func=write_project_file,
            name="write_project_file",
            description=(
                "Create a NEW file or completely overwrite a file with full content. "
                "Use for new .tex files (e.g. abstract.tex) or full rewrites. "
                "For small changes to an existing file, use edit_project_file instead."
            ),
            args_schema=WriteArgs,
        ),
        StructuredTool.from_function(
            func=edit_project_file,
            name="edit_project_file",
            description=(
                "Edit an existing file by exact string replacement (old_str -> new_str). "
                "old_str must match exactly once. Always read_project_file first."
            ),
            args_schema=EditArgs,
        ),
    ]
