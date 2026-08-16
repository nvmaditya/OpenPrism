"""Map LangGraph agent runs to typed stream events (SSE payload builders)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from datetime import datetime, timezone
from uuid import uuid4

from app.agent.graph import build_agent
from app.agent.verify import (
    load_tex_map,
    project_structure,
    restore_text_files,
    snapshot_text_files,
    verify_project,
)
from app.compile.engine import CompileEngine
from app.domain.project import ProjectStore
from app.settings import Settings

_WRITE_OK = re.compile(
    r"OK (?:wrote|edited) (?P<path>\S+)"
)


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
            if name in (
                "write_project_file",
                "edit_project_file",
                "add_tex_input",
            ) and isinstance(args, dict):
                path = args.get("path")
                if path:
                    op = "edit" if name == "edit_project_file" else "write"
                    out.append(("file_changed", {"path": path, "op": op}))
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


def _format_agent_log(
    *,
    trace_id: str,
    user_message: str,
    tools: list[dict[str, Any]],
    files_touched: list[str],
    verify: dict[str, Any],
    rollback: bool,
    excerpt: str,
) -> str:
    lines = [
        f"trace_id={trace_id}",
        f"verified={verify.get('ok')}",
        f"rollback={rollback}",
        f"user={user_message}",
        "tools:",
    ]
    if tools:
        for t in tools:
            lines.append(f"  - {t.get('name')}: {str(t.get('result', ''))[:400]}")
    else:
        lines.append("  (none)")
    lines.append("files: " + (", ".join(files_touched) if files_touched else "(none)"))
    lines.append(f"verify: {verify}")
    if excerpt:
        lines.append("--- tex excerpt ---")
        lines.append(excerpt[-4000:])
    return "\n".join(lines) + "\n"


def iter_agent_events(
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
) -> Iterator[tuple[str, dict[str, Any]]]:
    """
    Run the project agent and yield (event_name, data) pairs.

    Always ends with a ``done`` event (ok true/false).
    After the ReAct loop, a host-side verify hook runs (cannot be skipped).
    """
    store.get(project_id)
    engine = compile_engine or CompileEngine(store)
    snap = snapshot_text_files(store, project_id)
    before = project_structure(load_tex_map(store, project_id))
    trace_id = uuid4().hex
    tools_used: list[dict[str, Any]] = []
    files_touched: list[str] = []

    graph = build_agent(
        store,
        project_id,
        model=model,
        settings=settings,
        system_prompt=system_prompt,
        open_path=open_path,
        open_preview=open_preview,
        compile_engine=engine,
    )

    seen_file_paths: set[str] = set()
    run_ok = True
    run_error: str | None = None
    try:
        stream = graph.stream(
            {"messages": [HumanMessage(content=user_message)]},
            stream_mode="updates",
        )
        for chunk in stream:
            for msg in _iter_update_messages(chunk):
                for event, data in _events_from_message(msg):
                    if event == "tool_end":
                        tools_used.append(
                            {
                                "name": data.get("name"),
                                "result": data.get("result"),
                            }
                        )
                    if event == "file_changed":
                        path = data.get("path")
                        if path in seen_file_paths:
                            continue
                        if path:
                            seen_file_paths.add(str(path))
                            files_touched.append(str(path))
                    yield event, data
    except Exception as exc:  # noqa: BLE001 — surface to client stream
        run_ok = False
        run_error = str(exc)
        yield "error", {"message": run_error}

    verify = verify_project(
        store, project_id, engine, before_structure=before
    )
    # Only restore when the document itself failed checks — not when the
    # model crashed without a bad write (trace fdd141f6).
    rollback = False
    if not verify.ok:
        restore_text_files(store, project_id, snap)
        rollback = True

    meta = {
        "log_id": trace_id,
        "project_id": project_id,
        "kind": "agent",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "success": bool(verify.ok and run_ok),
        "verified": bool(verify.ok),
        "rollback": rollback,
        "error": run_error,
        "user_message": user_message[:2000],
        "files_touched": files_touched,
        "tools": tools_used,
        "verify": verify.to_dict(),
    }
    log_text = _format_agent_log(
        trace_id=trace_id,
        user_message=user_message,
        tools=tools_used,
        files_touched=files_touched,
        verify=verify.to_dict(),
        rollback=rollback,
        excerpt=verify.compile_excerpt
        or "\n".join(verify.static_errors + verify.structure_errors),
    )
    if run_error:
        log_text = f"error={run_error}\n" + log_text
    try:
        engine.write_log(project_id, trace_id, log_text, meta)
    except Exception:  # noqa: BLE001
        pass

    if rollback:
        yield (
            "token",
            {
                "text": (
                    f"\n\nVerification failed; files restored. "
                    f"Paste this trace: {trace_id}"
                )
            },
        )
    elif run_error:
        yield (
            "token",
            {
                "text": (
                    f"\n\nAgent error (document unchanged). "
                    f"Paste this trace: {trace_id}"
                )
            },
        )

    yield (
        "done",
        {
            "ok": run_ok,
            "project_id": project_id,
            "trace_id": trace_id,
            "verified": bool(verify.ok),
            "rollback": rollback,
            "error": run_error,
        },
    )


def iter_sse_bytes(
    store: ProjectStore,
    project_id: str,
    user_message: str,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    open_path: str | None = None,
    open_preview: str | None = None,
    compile_engine: CompileEngine | None = None,
) -> Iterator[bytes]:
    """Yield UTF-8 SSE frames for StreamingResponse."""
    for event, data in iter_agent_events(
        store,
        project_id,
        user_message,
        model=model,
        settings=settings,
        open_path=open_path,
        open_preview=open_preview,
        compile_engine=compile_engine,
    ):
        yield sse_frame(event, data).encode("utf-8")
