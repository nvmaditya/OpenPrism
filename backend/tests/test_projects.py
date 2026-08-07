"""Projects domain: CRUD, import, zip-slip, isolation via real FastAPI app."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def client(projects_root: Path) -> TestClient:
    settings = Settings(prism_projects_root=projects_root)
    app = create_app(settings)
    return TestClient(app)


def _ids(resp_json: dict) -> set[str]:
    return {p["id"] for p in resp_json["projects"]}


def test_create_list_delete_blank_and_example(client: TestClient, projects_root: Path):
    blank = client.post("/api/projects", json={"kind": "blank", "name": "Blank A"})
    assert blank.status_code == 200, blank.text
    blank_body = blank.json()
    assert blank_body["id"]
    assert blank_body["name"] == "Blank A"
    assert blank_body["kind"] == "blank"

    example = client.post("/api/projects", json={"kind": "example"})
    assert example.status_code == 200, example.text
    example_body = example.json()
    assert example_body["kind"] == "example"
    assert "id" in example_body and "name" in example_body

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    ids = _ids(listed.json())
    assert blank_body["id"] in ids
    assert example_body["id"] in ids

    # On-disk workspace exists
    assert (projects_root / blank_body["id"] / "main.tex").is_file()
    assert (projects_root / example_body["id"] / "main.tex").is_file()

    # blank main.tex readable via API
    main = client.get(f"/api/projects/{blank_body['id']}/files/main.tex")
    assert main.status_code == 200
    assert "Hello, Prism" in main.json()["content"]

    deleted = client.delete(f"/api/projects/{blank_body['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True

    listed2 = client.get("/api/projects")
    ids2 = _ids(listed2.json())
    assert blank_body["id"] not in ids2
    assert example_body["id"] in ids2
    assert not (projects_root / blank_body["id"]).exists()


def test_import_zip_and_read_file(client: TestClient, projects_root: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.tex", r"\documentclass{article}\begin{document}ZIPOK\end{document}")
        zf.writestr("notes/readme.txt", "from-zip")
    buf.seek(0)

    resp = client.post(
        "/api/projects/import",
        files={"file": ("paper.zip", buf.getvalue(), "application/zip")},
        data={"name": "From Zip"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "import"
    assert body["name"] == "From Zip"
    pid = body["id"]

    main = client.get(f"/api/projects/{pid}/files/main.tex")
    assert main.status_code == 200
    assert "ZIPOK" in main.json()["content"]

    note = client.get(f"/api/projects/{pid}/files/notes/readme.txt")
    assert note.status_code == 200
    assert note.json()["content"] == "from-zip"

    assert (projects_root / pid / "main.tex").is_file()


def test_zip_slip_rejected_no_escape(client: TestClient, projects_root: Path, tmp_path: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
        zf.writestr("safe.tex", "ok")
    buf.seek(0)

    before = set(projects_root.iterdir()) if projects_root.exists() else set()
    outside_marker = projects_root.parent / "evil.txt"
    if outside_marker.exists():
        outside_marker.unlink()

    resp = client.post(
        "/api/projects/import",
        files={"file": ("evil.zip", buf.getvalue(), "application/zip")},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "zip_slip"

    assert not outside_marker.exists()
    # No new project dirs left for failed import
    after = set(projects_root.iterdir()) if projects_root.exists() else set()
    # failed import should not leave a committed project with meta
    new_dirs = after - before
    for d in new_dirs:
        if d.is_dir():
            assert not (d / ".prism" / "meta.json").is_file()


def test_cross_project_isolation(client: TestClient):
    a = client.post("/api/projects", json={"kind": "blank", "name": "A"}).json()
    b = client.post("/api/projects", json={"kind": "blank", "name": "B"}).json()
    secret = "SECRET_ONLY_IN_A_42"

    w = client.put(
        f"/api/projects/{a['id']}/files/secret.txt",
        json={"content": secret},
    )
    assert w.status_code == 200

    # A can read
    ra = client.get(f"/api/projects/{a['id']}/files/secret.txt")
    assert ra.status_code == 200
    assert ra.json()["content"] == secret

    # B cannot read A's file under B's id
    rb = client.get(f"/api/projects/{b['id']}/files/secret.txt")
    assert rb.status_code == 404

    files_b = client.get(f"/api/projects/{b['id']}/files")
    assert files_b.status_code == 200
    assert "secret.txt" not in files_b.json()["files"]

    # Path escape via .. rejected
    esc = client.get(f"/api/projects/{b['id']}/files/../{a['id']}/secret.txt")
    assert esc.status_code in (400, 404)


def test_health_still_ok(client: TestClient):
    r1 = client.get("/health")
    r2 = client.get("/health")
    assert r1.status_code == 200 and r1.json()["status"] == "ok"
    assert r2.status_code == 200 and r2.json()["status"] == "ok"
