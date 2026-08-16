"""LangGraph agent entry: project-scoped FS tools + pluggable chat model."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from app.agent.providers import get_chat_model
from app.agent.tools_fs import make_fs_tools
from app.agent.tools_tex import make_tex_tools
from app.compile.engine import CompileEngine
from app.domain.project import ProjectStore
from app.settings import Settings, get_settings

DEFAULT_SYSTEM = """You are the Prism research writing agent inside a LaTeX project workspace.

## Critical rules
1. To change any file you MUST call tools. Never only describe edits in chat.
2. Paths are relative to the project root (e.g. main.tex, sections/intro.tex). No absolute paths, no ..
3. Workflow for edits:
   - list_project_files if unsure what exists
   - read_project_file before changing an existing file
   - edit_project_file for small/partial changes (preferred for main.tex)
   - add_tex_input for a NEW section file plus one \\input in main.tex
   - write_project_file for other NEW files or full rewrites only
4. When creating a new .tex section file, write a fragment (no \\documentclass).
5. After tools succeed, briefly confirm what you wrote/changed (paths only). Do not dump entire huge files in chat unless asked.
6. If a tool returns ERROR, fix the approach (re-read, narrower old_str) and retry.
7. Valid LaTeX: balanced braces, proper environments, relative paths for \\includegraphics.
8. You may call check_latex, compile_project, and extract_pdf_text mid-turn. The host still verifies after you finish and will roll back a broken document.
"""

RESUME_ADDENDUM = """
## Resume / modular TeX
- Prefer edit_project_file on existing section files. Never rewrite main.tex wholesale.
- Keep the preamble and existing \\input/\\include lines. New sections: add_tex_input.
- Do not put \\documentclass inside sections/*.tex.
- Keep existing section headings unless the user asked to rename or remove them.
"""


def _wants_resume_prompt(store: ProjectStore, project_id: str) -> bool:
    try:
        info = store.get(project_id)
        if info.kind == "example":
            return True
        return any(p.startswith("sections/") for p in store.list_files(project_id))
    except Exception:  # noqa: BLE001
        return False


def build_system_prompt(
    store: ProjectStore,
    project_id: str,
    *,
    open_path: str | None = None,
    open_preview: str | None = None,
    base: str | None = None,
) -> str:
    """Augment system prompt with live project file list and optional open buffer."""
    text = base if base is not None else DEFAULT_SYSTEM
    if _wants_resume_prompt(store, project_id):
        text = text.rstrip() + "\n" + RESUME_ADDENDUM
    parts = [text.rstrip(), ""]
    try:
        files = store.list_files(project_id)
        if files:
            parts.append("## Current project files")
            for p in files[:80]:
                parts.append(f"- {p}")
            if len(files) > 80:
                parts.append(f"- … and {len(files) - 80} more")
        else:
            parts.append("## Current project files\n(empty)")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"(could not list files: {exc})")

    if open_path:
        parts.append("")
        parts.append(f"## User has open in the editor: `{open_path}`")
        if open_preview:
            preview = open_preview
            if len(preview) > 4000:
                preview = preview[:4000] + "\n… [truncated]"
            parts.append("### Editor buffer preview")
            parts.append("```")
            parts.append(preview)
            parts.append("```")
            parts.append(
                "Prefer edit_project_file on this path unless the user asks for a different file."
            )

    parts.append("")
    parts.append(
        "Remember: file changes only count if you call write_project_file, edit_project_file, or add_tex_input."
    )
    return "\n".join(parts)


def build_agent(
    store: ProjectStore,
    project_id: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    system_prompt: str | None = None,
    open_path: str | None = None,
    open_preview: str | None = None,
    compile_engine: CompileEngine | None = None,
):
    """
    Build a LangGraph ReAct agent bound to ``project_id``.

    FS tools wrap the real ``ProjectStore`` so path sandbox rules apply.
    """
    chat = model if model is not None else get_chat_model(settings or get_settings())
    engine = compile_engine or CompileEngine(store)
    tools = make_fs_tools(store, project_id) + make_tex_tools(store, project_id, engine)
    prompt = system_prompt or build_system_prompt(
        store,
        project_id,
        open_path=open_path,
        open_preview=open_preview,
    )

    # Prefer explicit system message via prompt callable / string
    try:
        return create_react_agent(chat, tools, prompt=prompt)
    except TypeError:
        pass
    try:
        return create_react_agent(
            chat,
            tools,
            state_modifier=SystemMessage(content=prompt),
        )
    except TypeError:
        return create_react_agent(chat, tools)


def run_agent(
    store: ProjectStore,
    project_id: str,
    user_message: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    system_prompt: str | None = None,
    open_path: str | None = None,
    open_preview: str | None = None,
    compile_engine: CompileEngine | None = None,
) -> dict[str, Any]:
    """Invoke the agent once for a user message."""
    graph = build_agent(
        store,
        project_id,
        model=model,
        settings=settings,
        system_prompt=system_prompt,
        open_path=open_path,
        open_preview=open_preview,
        compile_engine=compile_engine,
    )
    # Prepend system as HumanMessage context if graph ignored prompt (fallback)
    result = graph.invoke(
        {"messages": [HumanMessage(content=user_message)]},
    )
    return result


def last_ai_text(result: dict[str, Any]) -> str:
    """Extract last AI message text from a graph result."""
    messages: list[BaseMessage] = result.get("messages") or []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and getattr(msg, "type", None) == "ai":
            if isinstance(content, str):
                return content
            return str(content)
    return ""
