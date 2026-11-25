from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

from fastapi.testclient import TestClient

from prismtex.settings import Settings

PROJECT_KEYS = {"id", "name", "kind", "created_at", "updated_at", "root_file", "engine"}


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "prismtex", "version": "0.1.0.dev0"}


def test_docs_live_under_api(client: TestClient) -> None:
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/docs").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_create_list_get_delete(client: TestClient) -> None:
    assert client.get("/api/projects").json() == {"projects": []}
    blank = client.post("/api/projects", json={"kind": "blank"})
    assert blank.status_code == 201
    paper = client.post("/api/projects", json={"kind": "paper", "name": "Draft"}).json()
    resume = client.post("/api/projects", json={"kind": "resume"}).json()
    assert set(paper) == PROJECT_KEYS
    assert (paper["name"], paper["kind"], paper["root_file"], paper["engine"]) == (
        "Draft",
        "paper",
        "main.tex",
        "pdflatex",
    )
    assert resume["name"] == "Example résumé"
    listed = client.get("/api/projects").json()["projects"]
    assert [p["id"] for p in listed] == [resume["id"], paper["id"], blank.json()["id"]]
    assert client.get(f"/api/projects/{paper['id']}").json() == paper

    response = client.delete(f"/api/projects/{paper['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/api/projects/{paper['id']}").status_code == 404
    missing = client.delete(f"/api/projects/{paper['id']}")
    assert missing.json()["detail"]["code"] == "project_not_found"


def test_create_defaults_to_blank(client: TestClient) -> None:
    project = client.post("/api/projects", json={}).json()
    assert (project["kind"], project["name"]) == ("blank", "Untitled project")


def test_create_rejects_import_kind(client: TestClient) -> None:
    response = client.post("/api/projects", json={"kind": "import"})
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "kind"


def test_unknown_project(client: TestClient) -> None:
    for path in ("/api/projects/" + "0" * 32, "/api/projects/not-an-id"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "project_not_found"


def test_patch(client: TestClient) -> None:
    project = client.post("/api/projects", json={"kind": "paper"}).json()
    pid = project["id"]
    client.put(f"/api/projects/{pid}/files/alt.tex", json={"content": "\\documentclass{book}"})

    response = client.patch(
        f"/api/projects/{pid}",
        json={"name": "Renamed", "root_file": "alt.tex", "engine": "lualatex"},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["root_file"], body["engine"]) == ("Renamed", "alt.tex", "lualatex")

    # Fields left out stay as they are.
    assert client.patch(f"/api/projects/{pid}", json={}).json()["root_file"] == "alt.tex"
    # An explicit null clears the root file.
    assert (
        client.patch(f"/api/projects/{pid}", json={"root_file": None}).json()["root_file"] is None
    )


def test_patch_errors(client: TestClient) -> None:
    pid = client.post("/api/projects", json={}).json()["id"]

    response = client.patch(f"/api/projects/{pid}", json={"root_file": "missing.tex"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert (detail["code"], detail["field"], detail["reason"]) == (
        "root_file_invalid",
        "root_file",
        "not_found",
    )

    response = client.patch(f"/api/projects/{pid}", json={"root_file": "-main.tex"})
    assert response.json()["detail"]["reason"] == "leading_dash"

    response = client.patch(f"/api/projects/{pid}", json={"engine": "context"})
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "engine"

    response = client.patch(f"/api/projects/{pid}", json={"name": " "})
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "name"

    response = client.patch("/api/projects/" + "0" * 32, json={"name": "x"})
    assert response.status_code == 404


def test_import(client: TestClient) -> None:
    archive = zip_bytes({"thesis/main.tex": b"\\documentclass{report}", "thesis/a.bib": b""})
    response = client.post(
        "/api/projects/import",
        files={"file": ("thesis.zip", archive, "application/zip")},
        data={"name": "Thesis"},
    )
    assert response.status_code == 201
    project = response.json()
    assert (project["kind"], project["name"], project["root_file"]) == (
        "import",
        "Thesis",
        "main.tex",
    )
    files = client.get(f"/api/projects/{project['id']}/files").json()["files"]
    assert [f["path"] for f in files] == ["a.bib", "main.tex"]


def test_import_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/projects/import",
        files={"file": ("evil.zip", zip_bytes({"../evil.tex": b"x"}), "application/zip")},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert (detail["code"], detail["reason"]) == ("import_rejected", "zip_slip")
    assert client.get("/api/projects").json() == {"projects": []}


def test_import_archive_over_limit_is_413(
    make_client: Callable[..., TestClient], make_settings: Callable[..., Settings]
) -> None:
    client = make_client(settings=make_settings(import_max_mb=1))
    archive = zip_bytes({"main.tex": b"x"}) + b"\0" * (2**20)
    response = client.post(
        "/api/projects/import", files={"file": ("big.zip", archive, "application/zip")}
    )
    assert response.status_code == 413
    assert response.json()["detail"] == {
        "code": "payload_too_large",
        "message": "This is larger than the 1 MB limit.",
        "limit_mb": 1,
    }


def test_import_requires_a_file(client: TestClient) -> None:
    response = client.post("/api/projects/import", data={"name": "x"})
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "file"
