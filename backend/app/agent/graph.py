"""LangGraph agent entry: project-scoped FS tools + pluggable chat model."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from app.agent.providers import get_chat_model
from app.agent.tools_fs import make_fs_tools
from app.domain.project import ProjectStore
from app.settings import Settings, get_settings

DEFAULT_SYSTEM = (
    "You are the Prism research agent. You edit LaTeX and project files "
    "using tools. Only use relative paths inside the project. "
    "When asked to write or edit a file, call write_project_file. "
    "When asked to read, call read_project_file."
)


def build_agent(
    store: ProjectStore,
    project_id: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    system_prompt: str = DEFAULT_SYSTEM,
):
    """
    Build a LangGraph ReAct agent bound to ``project_id``.

    FS tools wrap the real ``ProjectStore`` so path sandbox rules apply.
    """
    chat = model if model is not None else get_chat_model(settings or get_settings())
    tools = make_fs_tools(store, project_id)
    # langgraph create_react_agent: prompt as system string (version-dependent)
    try:
        return create_react_agent(chat, tools, prompt=system_prompt)
    except TypeError:
        # Older/newer signature variants
        return create_react_agent(chat, tools)


def run_agent(
    store: ProjectStore,
    project_id: str,
    user_message: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    system_prompt: str = DEFAULT_SYSTEM,
) -> dict[str, Any]:
    """
    Invoke the agent once for a user message.

    Returns the graph result dict (includes ``messages``).
    """
    graph = build_agent(
        store,
        project_id,
        model=model,
        settings=settings,
        system_prompt=system_prompt,
    )
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
