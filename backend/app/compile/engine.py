"""Project compile orchestration + retrievable logs."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.domain.project import ProjectError, ProjectStore
from app.sandbox.process import (
    TEX_ALLOWLIST,
    ToolNotAllowlisted,
    ToolNotFound,
    run_allowlisted,
)

EngineName = Literal["latexmk", "pdflatex", "xelatex", "lualatex", "latex"]

LOG_DIR = "logs"  # under .prism/


@dataclass
class CompileResult:
    project_id: str
    log_id: str
    engine: str
    main_file: str
    success: bool
    returncode: int
    duration_ms: int
    log_text: str
    pdf_path: str | None

    def to_dict(self) -> dict[str, Any]:
        pdf_url = (
            f"/api/projects/{self.project_id}/pdf?path={self.pdf_path}"
            if self.pdf_path
            else None
        )
        return {
            "project_id": self.project_id,
            "log_id": self.log_id,
            "engine": self.engine,
            "main_file": self.main_file,
            "success": self.success,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "pdf_path": self.pdf_path,
            "pdf_available": bool(self.pdf_path),
            "pdf_url": pdf_url,
            "log_url": f"/api/projects/{self.project_id}/logs/{self.log_id}",
            # full log also persisted; include for convenience on compile response
            "log": self.log_text,
        }


class CompileEngine:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    def _project_dir(self, project_id: str) -> Path:
        # Reuse store validation
        info = self.store.get(project_id)
        return self.store.projects_root / info.id

    def _logs_dir(self, project_dir: Path) -> Path:
        d = project_dir / ".prism" / LOG_DIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_tool(
        self,
        project_id: str,
        tool: str,
        args: list[str] | None = None,
        *,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Run a single allowlisted tool in the project cwd; store a log."""
        project_dir = self._project_dir(project_id)
        args = args or []
        try:
            result = run_allowlisted(
                tool, args, cwd=project_dir, timeout_s=timeout_s
            )
        except ToolNotAllowlisted as exc:
            raise ProjectError(str(exc), code="not_allowlisted") from exc
        except ToolNotFound as exc:
            raise ProjectError(str(exc), code="tool_not_found") from exc

        log_id = uuid.uuid4().hex
        log_text = result.combined_log()
        meta = {
            "log_id": log_id,
            "project_id": project_id,
            "kind": "tool",
            "tool": tool,
            "args": args,
            "success": result.ok,
            "returncode": result.returncode,
            "duration_ms": result.duration_ms,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_log(project_dir, log_id, log_text, meta)
        return {**meta, "log": log_text}

    def compile(
        self,
        project_id: str,
        *,
        engine: EngineName = "latexmk",
        main_file: str = "main.tex",
        timeout_s: float = 180.0,
    ) -> CompileResult:
        """
        Compile ``main_file`` inside the project workspace.

        Logs are stored under ``.prism/logs/{log_id}`` (inside the project root).
        """
        if engine not in TEX_ALLOWLIST:
            raise ProjectError(f"engine not allowlisted: {engine}", code="not_allowlisted")

        # main_file must be a relative path inside project (sandbox)
        try:
            main_path = self.store.resolve_file(project_id, main_file)
        except ProjectError:
            raise
        if not main_path.is_file():
            raise ProjectError(
                f"main file not found: {main_file}", code="file_not_found"
            )

        project_dir = self._project_dir(project_id)
        args = self._engine_args(engine, main_file)

        try:
            result = run_allowlisted(
                engine, args, cwd=project_dir, timeout_s=timeout_s
            )
        except ToolNotAllowlisted as exc:
            raise ProjectError(str(exc), code="not_allowlisted") from exc
        except ToolNotFound as exc:
            raise ProjectError(str(exc), code="tool_not_found") from exc

        log_id = uuid.uuid4().hex
        log_text = result.combined_log()

        pdf_rel = None
        stem = Path(main_file).stem
        candidate = project_dir / f"{stem}.pdf"
        if candidate.is_file():
            pdf_rel = f"{stem}.pdf"

        success = result.ok and (
            pdf_rel is not None or engine in ("latex",)  # DVI workflow may not produce PDF
        )
        # For latex (DVI), success is returncode only
        if engine == "latex":
            success = result.ok
        elif engine in ("latexmk", "pdflatex", "xelatex", "lualatex"):
            success = result.ok and pdf_rel is not None

        meta = {
            "log_id": log_id,
            "project_id": project_id,
            "kind": "compile",
            "engine": engine,
            "main_file": main_file,
            "success": success,
            "returncode": result.returncode,
            "duration_ms": result.duration_ms,
            "pdf_path": pdf_rel,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_log(project_dir, log_id, log_text, meta)

        return CompileResult(
            project_id=project_id,
            log_id=log_id,
            engine=engine,
            main_file=main_file,
            success=success,
            returncode=result.returncode,
            duration_ms=result.duration_ms,
            log_text=log_text,
            pdf_path=pdf_rel,
        )

    def _engine_args(self, engine: EngineName, main_file: str) -> list[str]:
        if engine == "latexmk":
            return [
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                main_file,
            ]
        if engine in ("pdflatex", "xelatex", "lualatex", "latex"):
            return ["-interaction=nonstopmode", main_file]
        raise ProjectError(f"unsupported engine: {engine}", code="invalid_engine")

    def write_log(
        self,
        project_id: str,
        log_id: str,
        log_text: str,
        meta: dict[str, Any],
    ) -> None:
        """Persist a log (compile, tool, or agent) under ``.prism/logs``."""
        if not re.fullmatch(r"[0-9a-f]{32}", log_id):
            raise ProjectError(f"invalid log id: {log_id}", code="not_found")
        project_dir = self._project_dir(project_id)
        payload = dict(meta)
        payload.setdefault("log_id", log_id)
        payload.setdefault("project_id", project_id)
        self._write_log(project_dir, log_id, log_text, payload)

    def _write_log(
        self,
        project_dir: Path,
        log_id: str,
        log_text: str,
        meta: dict[str, Any],
    ) -> None:
        logs = self._logs_dir(project_dir)
        (logs / f"{log_id}.log").write_text(log_text, encoding="utf-8", errors="replace")
        (logs / f"{log_id}.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    def get_log(self, project_id: str, log_id: str) -> dict[str, Any]:
        project_dir = self._project_dir(project_id)
        if not re.fullmatch(r"[0-9a-f]{32}", log_id):
            raise ProjectError(f"invalid log id: {log_id}", code="not_found")
        logs = self._logs_dir(project_dir)
        meta_path = logs / f"{log_id}.json"
        log_path = logs / f"{log_id}.log"
        if not meta_path.is_file() or not log_path.is_file():
            raise ProjectError(f"log not found: {log_id}", code="not_found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["log"] = log_path.read_text(encoding="utf-8", errors="replace")
        return meta

    def list_logs(self, project_id: str) -> list[dict[str, Any]]:
        project_dir = self._project_dir(project_id)
        logs = self._logs_dir(project_dir)
        items: list[dict[str, Any]] = []
        for meta_path in sorted(logs.glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta.pop("log", None)
                items.append(meta)
            except (OSError, json.JSONDecodeError):
                continue
        return items
