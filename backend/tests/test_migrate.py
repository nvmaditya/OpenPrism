from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from prismtex.layout import DataLayout
from prismtex.migrate import migrate_legacy_projects
from prismtex.settings import Settings
from prismtex.workspace import ProjectStore

LEGACY_ID = "a" * 32


def make_legacy(layout: DataLayout, project_id: str, meta: object, files: dict[str, bytes]) -> Path:
    project_dir = layout.project_dir(project_id)
    (project_dir / ".prism" / "logs").mkdir(parents=True)
    (project_dir / ".prism" / "logs" / "old.log").write_bytes(b"old log")
    (project_dir / ".prism" / "meta.json").write_bytes(json.dumps(meta).encode())
    for rel, data in files.items():
        (project_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (project_dir / rel).write_bytes(data)
    return project_dir


def layout_at(tmp_path: Path) -> DataLayout:
    layout = DataLayout(tmp_path / "data")
    layout.ensure()
    return layout


def test_migrates_example_project(tmp_path: Path) -> None:
    layout = layout_at(tmp_path)
    meta = {
        "id": LEGACY_ID,
        "name": "Old resume",
        "kind": "example",
        "created_at": "2026-01-02T03:04:05+00:00",
    }
    project_dir = make_legacy(layout, LEGACY_ID, meta, {"main.tex": b"\\documentclass{article}"})

    assert migrate_legacy_projects(layout) == [LEGACY_ID]

    assert not (project_dir / ".prism").exists()
    assert (project_dir / "main.tex").exists()
    info = ProjectStore(layout).get(LEGACY_ID)
    assert (info.name, info.kind, info.root_file, info.engine) == (
        "Old resume",
        "resume",
        "main.tex",
        "pdflatex",
    )
    assert info.created_at == "2026-01-02T03:04:05+00:00"
    saved = json.loads(layout.project_json(LEGACY_ID).read_text(encoding="utf-8"))
    assert saved["schema"] == 1


def test_is_idempotent(tmp_path: Path) -> None:
    layout = layout_at(tmp_path)
    make_legacy(layout, LEGACY_ID, {"name": "x", "kind": "blank"}, {"main.tex": b""})
    assert migrate_legacy_projects(layout) == [LEGACY_ID]
    before = layout.project_json(LEGACY_ID).read_bytes()
    assert migrate_legacy_projects(layout) == []
    assert layout.project_json(LEGACY_ID).read_bytes() == before


def test_root_file_detection_and_kinds(tmp_path: Path) -> None:
    layout = layout_at(tmp_path)
    make_legacy(
        layout,
        "b" * 32,
        {"name": "Imported", "kind": "import", "created_at": "2026-01-01T00:00:00+00:00"},
        {"paper.tex": b"\\documentclass{article}", "notes.tex": b"notes"},
    )
    make_legacy(layout, "c" * 32, {"name": "Blank", "kind": "blank"}, {"x.txt": b"x"})
    make_legacy(layout, "d" * 32, "not an object", {"main.tex": b""})
    assert sorted(migrate_legacy_projects(layout)) == ["b" * 32, "c" * 32, "d" * 32]
    store = ProjectStore(layout)
    imported = store.get("b" * 32)
    assert (imported.kind, imported.root_file) == ("import", "paper.tex")
    blank = store.get("c" * 32)
    assert (blank.kind, blank.root_file) == ("blank", None)
    broken = store.get("d" * 32)
    assert (broken.name, broken.kind, broken.root_file) == (
        "Untitled project",
        "import",
        "main.tex",
    )


def test_existing_project_json_is_kept(tmp_path: Path) -> None:
    layout = layout_at(tmp_path)
    store = ProjectStore(layout)
    info = store.create(name="Current")
    legacy = layout.project_dir(info.id) / ".prism"
    legacy.mkdir()
    (legacy / "meta.json").write_bytes(b'{"name": "Stale", "kind": "example"}')
    assert migrate_legacy_projects(layout) == [info.id]
    assert store.get(info.id) == info
    assert not legacy.exists()


def test_other_folders_are_left_alone(tmp_path: Path) -> None:
    layout = layout_at(tmp_path)
    plain = layout.project_dir("e" * 32)
    plain.mkdir()
    (plain / "main.tex").write_bytes(b"x")
    odd = layout.projects / "not-a-project-id"
    (odd / ".prism").mkdir(parents=True)
    (odd / ".prism" / "meta.json").write_bytes(b"{}")
    no_meta = layout.project_dir("f" * 32)
    (no_meta / ".prism").mkdir(parents=True)

    assert migrate_legacy_projects(layout) == []
    assert (plain / "main.tex").exists()
    assert (odd / ".prism" / "meta.json").exists()
    assert (no_meta / ".prism").exists()
    assert not layout.state("e" * 32).exists()


def test_create_app_migrates_at_startup(
    make_client: Callable[..., TestClient], settings: Settings
) -> None:
    layout = DataLayout(settings.data_dir)
    layout.ensure()
    make_legacy(
        layout,
        LEGACY_ID,
        {"name": "Legacy", "kind": "example", "created_at": "2026-01-01T00:00:00+00:00"},
        {"main.tex": b"\\documentclass{article}"},
    )
    client = make_client()
    projects = client.get("/api/projects").json()["projects"]
    assert [(p["id"], p["name"], p["kind"]) for p in projects] == [(LEGACY_ID, "Legacy", "resume")]
    files = client.get(f"/api/projects/{LEGACY_ID}/files").json()["files"]
    assert [f["path"] for f in files] == ["main.tex"]
