"""Where everything lives under the data directory (ADR 0004).

A project folder holds only the user's files. Project state, build output
and history live beside it, out of reach of the file API.
"""

from __future__ import annotations

from pathlib import Path


class DataLayout:
    """Paths under one data directory. Methods do not validate project ids."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir).resolve()
        self.projects = self.root / "projects"
        self.state_root = self.root / "state"
        self.build_root = self.root / "build"
        self.history_root = self.root / "history"
        self.settings_json = self.root / "settings.json"
        self.ledger = self.state_root / "ledger.jsonl"

    def project_dir(self, project_id: str) -> Path:
        return self.projects / project_id

    def state(self, project_id: str) -> Path:
        return self.state_root / project_id

    def project_json(self, project_id: str) -> Path:
        return self.state(project_id) / "project.json"

    def logs(self, project_id: str) -> Path:
        return self.state(project_id) / "logs"

    def build(self, project_id: str) -> Path:
        return self.build_root / project_id

    def out(self, project_id: str) -> Path:
        return self.build(project_id) / "out"

    def history_git(self, project_id: str) -> Path:
        return self.history_root / f"{project_id}.git"

    def ensure(self) -> None:
        for path in (self.projects, self.state_root, self.build_root, self.history_root):
            path.mkdir(parents=True, exist_ok=True)
