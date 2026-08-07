"""Map LangGraph agent runs to typed stream events (SSE payload builders)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.graph import DEFAULT_SYSTEM, build_agent
from app.domain.project import ProjectStore
from app.settings import Settings

_WRITE_OK = re.compile(r"OK wrote (?P<path>\S+)")


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """Serialize one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _events_from_message(msg: BaseMessage) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    msg_type = getattr(msg, "type", None) or msg.__class__.__name__.lower()

    if isinstance(msg, AIMessage) or msg_type == "ai":
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            out.append(
                (
                    "tool_start",
                    {"name": name, "args": args or {}, "id": tc_id},
                )
            )
            if name == "write_project_file" and isinstance(args, dict):
                path = args.get("path")
                if path:
                    out.append(
                        (
                            "file_changed",
                            {"path": path, "op": "write"},
                        )
                    )
        content = getattr(msg, "content", None) or ""
        if isinstance(content, list):
            # content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text") or "")
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "".join(text_parts)
        if content:
            out.append(("token", {"text": str(content)}))
        return out

    if isinstance(msg, ToolMessage) or msg_type == "tool":
        name = getattr(msg, "name", None) or ""
        content = str(getattr(msg, "content", "") or "")
        out.append(
            (
                "tool_end",
                {"name": name, "result": content[:2000]},
            )
        )
        m = _WRITE_OK.search(content)
        if m:
            out.append(
                (
                    "file_changed",
                    {"path": m.group("path"), "op": "write"},
                )
            )
        return out

    return out


def _iter_update_messages(update: Any) -> Iterator[BaseMessage]:
    """Yield messages from a LangGraph stream 'updates' chunk."""
    if not isinstance(update, dict):
        return
    for _node, payload in update.items():
        if payload is None:
            continue
        if isinstance(payload, dict) and "messages" in payload:
            msgs = payload["messages"]
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, BaseMessage):
                        yield m
            elif isinstance(msgs, BaseMessage):
                yield msgs
        elif isinstance(payload, list):
            for m in payload:
                if isinstance(m, BaseMessage):
                    yield m
        elif isinstance(payload, BaseMessage):
            yield payload


def iter_agent_events(
    store: ProjectStore,
    project_id: str,
    user_message: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    system_prompt: str = DEFAULT_SYSTEM,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """
    Run the project agent and yield (event_name, data) pairs.

    Always ends with a ``done`` event (ok true/false).
    """
    # Validate project exists early
    store.get(project_id)

    graph = build_agent(
        store,
        project_id,
        model=model,
        settings=settings,
        system_prompt=system_prompt,
    )

    seen_file_paths: set[str] = set()
    try:
        stream = graph.stream(
            {"messages": [HumanMessage(content=user_message)]},
            stream_mode="updates",
        )
        for chunk in stream:
            for msg in _iter_update_messages(chunk):
                for event, data in _events_from_message(msg):
                    if event == "file_changed":
                        path = data.get("path")
                        if path in seen_file_paths:
                            continue
                        if path:
                            seen_file_paths.add(str(path))
                    yield event, data
        yield "done", {"ok": True, "project_id": project_id}
    except Exception as exc:  # noqa: BLE001 — surface to client stream
        yield "error", {"message": str(exc)}
        yield "done", {"ok": False, "project_id": project_id}


def iter_sse_bytes(
    store: ProjectStore,
    project_id: str,
    user_message: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
) -> Iterator[bytes]:
    """Yield UTF-8 SSE frames for StreamingResponse."""
    for event, data in iter_agent_events(
        store,
        project_id,
        user_message,
        model=model,
        settings=settings,
    ):
        yield sse_frame(event, data).encode("utf-8")
