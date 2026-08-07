"""Phase 5: PDF serve + preview isolation under preview root."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

HAS_PDFLATEX = shutil.which("pdflatex") is not None
FIXTURE_PDF = Path(__file__).parent / "fixtures" / "minimal.pdf"


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "projects"
    previews = tmp_path / "previews"
    projects.mkdir()
    previews.mkdir()
    return projects, previews


@pytest.fixture
def client(roots: tuple[Path, Path]) -> TestClient:
    projects, previews = roots
    return TestClient(
        create_app(
            Settings(
                prism_projects_root=projects,
                prism_preview_root=previews,
            )
        )
    )


def _create(client: TestClient) -> str:
    r = client.post("/api/projects", json={"kind": "blank", "name": "Pdf"})
    assert r.status_code == 200
    return r.json()["id"]


def _seed_pdf(client: TestClient, pid: str, name: str = "main.pdf") -> None:
    assert FIXTURE_PDF.is_file(), "minimal.pdf fixture missing"
    data = FIXTURE_PDF.read_bytes()
    assert data.startswith(b"%PDF")
    # upload via API
    up = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": (name, data, "application/pdf")},
        data={"path": name if "/" not in name else name},
    )
    # upload with path=main.pdf treats as file path when suffix present
    if up.status_code != 200:
        # write via put as base64-unfriendly; use store path through write of bytes
        # Fallback: put only supports text — use upload to prism-uploads then... 
        pass
    # Always write via raw project store path using upload dest
    if name == "main.pdf":
        up2 = client.post(
            f"/api/projects/{pid}/upload",
            files={"file": ("main.pdf", data, "application/pdf")},
            data={"path": "main.pdf"},
        )
        assert up2.status_code == 200, up2.text
        assert up2.json()["path"] == "main.pdf"


def test_serve_fixture_pdf_bytes(client: TestClient, roots: tuple[Path, Path]):
    projects, _ = roots
    pid = _create(client)
    _seed_pdf(client, pid, "main.pdf")

    r = client.get(f"/api/projects/{pid}/pdf", params={"path": "main.pdf"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF")
    assert (projects / pid / "main.pdf").is_file()


def test_compile_returns_pdf_url_and_log_when_tex(client: TestClient):
    if not HAS_PDFLATEX:
        pytest.skip("pdflatex not on PATH")
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/compile",
        json={"engine": "pdflatex", "main_file": "main.tex"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["log_id"]
    assert body.get("pdf_available") is True
    assert body.get("pdf_path") == "main.pdf"
    assert body.get("pdf_url") == f"/api/projects/{pid}/pdf?path=main.pdf"
    assert body.get("log_url") == f"/api/projects/{pid}/logs/{body['log_id']}"

    pdf = client.get(body["pdf_url"])
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    log = client.get(body["log_url"])
    assert log.status_code == 200
    assert log.json()["log_id"] == body["log_id"]


def test_preview_renders_under_preview_root_not_project(
    client: TestClient, roots: tuple[Path, Path]
):
    projects, previews = roots
    pid = _create(client)
    _seed_pdf(client, pid)

    r = client.post(
        f"/api/projects/{pid}/previews/render",
        json={"pdf_path": "main.pdf", "dpi": 36, "max_pages": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page_count"] >= 1
    assert body["run_id"]
    run_id = body["run_id"]

    # pages on disk under preview root
    run_dir = previews / pid / run_id
    assert run_dir.is_dir()
    page_files = list(run_dir.glob("page-*.png"))
    assert page_files, "expected page-*.png under preview root"

    # MUST NOT write page rasters into project workspace
    project_pages = list((projects / pid).rglob("page-*.png"))
    project_pages += list((projects / pid).rglob("*-page-*.png"))
    assert project_pages == [], f"preview leaked into project: {project_pages}"

    # serve page image
    name = body["pages"][0]
    img = client.get(f"/api/projects/{pid}/previews/{run_id}/{name}")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/png")
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_preview_path_escape_rejected(client: TestClient, roots: tuple[Path, Path]):
    from app.previews.pdf_render import PreviewService
    from app.domain.project import ProjectStore

    projects, previews = roots
    pid = _create(client)
    _seed_pdf(client, pid)

    # Absolute / .. pdf source path rejected on render
    abs_pdf = client.post(
        f"/api/projects/{pid}/previews/render",
        json={"pdf_path": "/etc/passwd.pdf"},
    )
    assert abs_pdf.status_code == 400, abs_pdf.text
    assert abs_pdf.json()["detail"]["code"] == "path_escape"

    dotdot_pdf = client.post(
        f"/api/projects/{pid}/previews/render",
        json={"pdf_path": "../secret.pdf"},
    )
    assert dotdot_pdf.status_code == 400
    assert dotdot_pdf.json()["detail"]["code"] == "path_escape"

    # Pure unit: resolve_preview_file rejects escapes (API single-segment filename
    # cannot carry / after URL routing, so test the real service entry).
    store = ProjectStore(projects)
    svc = PreviewService(store, previews)
    with pytest.raises(Exception) as ei:
        svc.resolve_preview_file(pid, "../outside.png")
    assert getattr(ei.value, "code", None) == "path_escape" or "escape" in str(ei.value).lower()

    with pytest.raises(Exception) as ei2:
        svc.resolve_preview_file(pid, "/abs/page-1.png")
    assert getattr(ei2.value, "code", None) == "path_escape"

    # Successful render still does not pollute project tree
    rendered = client.post(
        f"/api/projects/{pid}/previews/render",
        json={"pdf_path": "main.pdf"},
    )
    assert rendered.status_code == 200
    assert list((projects / pid).rglob("page-*.png")) == []
    assert list((previews / pid).rglob("page-*.png"))


def test_health(client: TestClient):
    assert client.get("/health").status_code == 200
