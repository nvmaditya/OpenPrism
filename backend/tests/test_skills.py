"""Phase 6: progressive skills list/load/install/create/plugin scaffold."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

REQUIRED = {
    "imagegen",
    "openai-docs",
    "plugin-creator",
    "skill-creator",
    "skill-installer",
}


@pytest.fixture
def skill_home(tmp_path: Path) -> Path:
    d = tmp_path / "skill-home"
    d.mkdir()
    return d


@pytest.fixture
def client(tmp_path: Path, skill_home: Path) -> TestClient:
    projects = tmp_path / "projects"
    previews = tmp_path / "previews"
    projects.mkdir()
    previews.mkdir()
    return TestClient(
        create_app(
            Settings(
                prism_projects_root=projects,
                prism_preview_root=previews,
                prism_skill_home=skill_home,
            )
        )
    )


def test_list_five_catalog_skills(client: TestClient):
    r = client.get("/api/skills")
    assert r.status_code == 200, r.text
    names = {s["name"] for s in r.json()["skills"]}
    assert REQUIRED.issubset(names), f"missing {REQUIRED - names}"
    assert r.json()["count"] >= 5


def test_load_skill_body(client: TestClient):
    r = client.get("/api/skills/imagegen")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "imagegen"
    assert body["body"]
    assert "imagegen" in body["body"].lower() or "raster" in body["body"].lower()
    assert "raw" in body and "---" in body["raw"]


def test_load_unknown_404(client: TestClient):
    r = client.get("/api/skills/not-a-real-skill-xyz")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_install_from_catalog_into_skill_home(
    client: TestClient, skill_home: Path
):
    r = client.post("/api/skills/install", json={"name": "openai-docs"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["source"] == "skill-home"
    assert (skill_home / "openai-docs" / "SKILL.md").is_file()

    loaded = client.get("/api/skills/openai-docs")
    assert loaded.status_code == 200
    assert loaded.json()["source"] == "skill-home"
    assert "openai" in loaded.json()["body"].lower()


def test_create_skill_and_list(client: TestClient, skill_home: Path):
    r = client.post(
        "/api/skills/create",
        json={
            "name": "my-custom-skill",
            "description": "A custom test skill for OpenPrism",
            "body": "# my-custom-skill\n\nDo the custom thing carefully.\n",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert (skill_home / "my-custom-skill" / "SKILL.md").is_file()

    listed = client.get("/api/skills")
    names = {s["name"] for s in listed.json()["skills"]}
    assert "my-custom-skill" in names

    loaded = client.get("/api/skills/my-custom-skill")
    assert loaded.status_code == 200
    assert "custom thing" in loaded.json()["body"].lower()


def test_plugin_scaffold_in_project(client: TestClient, tmp_path: Path):
    proj = client.post("/api/projects", json={"kind": "blank", "name": "Plug"}).json()
    pid = proj["id"]
    r = client.post(
        "/api/skills/plugin-scaffold",
        json={
            "name": "demo-plugin",
            "project_id": pid,
            "description": "Demo plugin",
            "version": "0.1.0",
        },
    )
    assert r.status_code == 200, r.text
    manifest = Path(r.json()["manifest_path"])
    assert manifest.is_file()
    assert manifest.name == "plugin.json"
    assert ".codex-plugin" in str(manifest)
    text = manifest.read_text(encoding="utf-8")
    assert "demo-plugin" in text
    # under project root
    assert pid in str(manifest)


def test_plugin_scaffold_default_under_skill_home(
    client: TestClient, skill_home: Path
):
    r = client.post(
        "/api/skills/plugin-scaffold",
        json={"name": "home-plugin", "description": "Under skill-home"},
    )
    assert r.status_code == 200, r.text
    manifest = Path(r.json()["manifest_path"])
    assert manifest.is_file()
    assert skill_home.resolve() in manifest.resolve().parents or str(
        skill_home
    ) in str(manifest)


def test_health(client: TestClient):
    assert client.get("/health").status_code == 200
