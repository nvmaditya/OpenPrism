"""Phase 2: structured tree, upload, path sandbox on real FastAPI handlers."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sandbox.paths import PathEscapeError, resolve_under
from app.settings import Settings


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def client(projects_root: Path) -> TestClient:
    return TestClient(create_app(Settings(prism_projects_root=projects_root)))


def _create(client: TestClient, name: str = "P") -> str:
    r = client.post("/api/projects", json={"kind": "blank", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _find_paths(node: dict) -> set[str]:
    found: set[str] = set()
    if node.get("path"):
        found.add(node["path"])
    for child in node.get("children") or []:
        found |= _find_paths(child)
        if child.get("type") == "file":
            found.add(child["path"])
        elif child.get("type") == "dir" and child.get("path"):
            found.add(child["path"])
    return found


def test_tree_is_structured_not_flat(client: TestClient, projects_root: Path):
    pid = _create(client)
    # nest a file
    w = client.put(
        f"/api/projects/{pid}/files/figs/plot.txt",
        json={"content": "nested"},
    )
    assert w.status_code == 200

    tree_resp = client.get(f"/api/projects/{pid}/tree")
    assert tree_resp.status_code == 200, tree_resp.text
    body = tree_resp.json()
    assert "tree" in body
    root = body["tree"]
    assert root["type"] == "dir"
    assert "children" in root
    # children should include dirs and files as objects, not a flat string list
    assert isinstance(root["children"], list)
    assert all(isinstance(c, dict) for c in root["children"])
    types = {c["type"] for c in root["children"]}
    assert "file" in types or "dir" in types

    paths = _find_paths(root)
    assert "main.tex" in paths
    assert "figs/plot.txt" in paths
    assert "prism-uploads" in paths or any(
        c.get("name") == "prism-uploads" for c in root["children"]
    )

    # flat list still works (Phase 1)
    flat = client.get(f"/api/projects/{pid}/files")
    assert flat.status_code == 200
    assert "main.tex" in flat.json()["files"]


def test_upload_binary_and_read_back(client: TestClient, projects_root: Path):
    pid = _create(client)
    payload = bytes([0, 1, 2, 3, 255, 10]) + b"PNG-ISH"

    up = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("profile.bin", payload, "application/octet-stream")},
    )
    assert up.status_code == 200, up.text
    meta = up.json()
    assert meta["ok"] is True
    assert meta["path"] == "prism-uploads/profile.bin"
    assert meta["size"] == len(payload)

    # tree shows upload
    tree = client.get(f"/api/projects/{pid}/tree").json()["tree"]
    paths = _find_paths(tree)
    assert "prism-uploads/profile.bin" in paths

    # read returns same bytes (base64 for binary)
    rd = client.get(f"/api/projects/{pid}/files/prism-uploads/profile.bin")
    assert rd.status_code == 200
    body = rd.json()
    if body["encoding"] == "base64":
        import base64

        assert base64.b64decode(body["content"]) == payload
    else:
        assert body["content"].encode("utf-8") == payload

    on_disk = projects_root / pid / "prism-uploads" / "profile.bin"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == payload


def test_upload_with_dest_path(client: TestClient, projects_root: Path):
    pid = _create(client)
    text = b"hello asset"
    up = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("note.txt", text, "text/plain")},
        data={"path": "assets"},
    )
    assert up.status_code == 200, up.text
    assert up.json()["path"] == "assets/note.txt"
    assert (projects_root / pid / "assets" / "note.txt").read_bytes() == text

    rd = client.get(f"/api/projects/{pid}/files/assets/note.txt")
    assert rd.status_code == 200
    assert rd.json()["content"] == "hello asset"


def test_path_escape_write_read_upload_tree(
    client: TestClient, projects_root: Path, tmp_path: Path
):
    from urllib.parse import quote

    pid = _create(client)
    outside = projects_root.parent / "escaped.txt"
    if outside.exists():
        outside.unlink()

    # URL-normalized ../ would never hit the handler; use percent-encoded segments
    # so FastAPI still delivers ".." into the path param.
    escape_rel = quote("../escaped.txt", safe="")
    w = client.put(
        f"/api/projects/{pid}/files/{escape_rel}",
        json={"content": "nope"},
    )
    assert w.status_code == 400, w.text
    assert w.json()["detail"]["code"] == "path_escape"
    assert not outside.exists()

    # nested .. also rejected
    w_nested = client.put(
        f"/api/projects/{pid}/files/{quote('figs/../../escaped.txt', safe='')}",
        json={"content": "nope"},
    )
    assert w_nested.status_code == 400
    assert w_nested.json()["detail"]["code"] == "path_escape"
    assert not outside.exists()

    # absolute-style path (posix) via encoded path
    w2 = client.put(
        f"/api/projects/{pid}/files/{quote('/etc/passwd', safe='')}",
        json={"content": "nope"},
    )
    assert w2.status_code == 400, w2.text
    assert w2.json()["detail"]["code"] == "path_escape"

    # Windows absolute style
    w3 = client.put(
        f"/api/projects/{pid}/files/{quote('C:/Windows/system32/x.txt', safe='')}",
        json={"content": "nope"},
    )
    assert w3.status_code == 400
    assert w3.json()["detail"]["code"] == "path_escape"

    # read escape
    r = client.get(f"/api/projects/{pid}/files/{quote('../../etc/passwd', safe='')}")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "path_escape"

    # upload escape via dest (relative ..)
    up = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("x.bin", b"x", "application/octet-stream")},
        data={"path": "../outside.bin"},
    )
    assert up.status_code == 400
    assert up.json()["detail"]["code"] == "path_escape"
    assert not (projects_root.parent / "outside.bin").exists()

    # absolute POSIX dest must 4xx — not silently rewrite to project-relative etc/passwd
    up_abs = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("passwd", b"pwn", "application/octet-stream")},
        data={"path": "/etc/passwd"},
    )
    assert up_abs.status_code == 400, up_abs.text
    assert up_abs.json()["detail"]["code"] == "path_escape"
    assert not (projects_root / pid / "etc" / "passwd").exists()

    up_abs_dir = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("x.bin", b"x", "application/octet-stream")},
        data={"path": "/abs/dir/"},
    )
    assert up_abs_dir.status_code == 400, up_abs_dir.text
    assert up_abs_dir.json()["detail"]["code"] == "path_escape"
    assert not (projects_root / pid / "abs").exists()

    up_win = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("x.bin", b"x", "application/octet-stream")},
        data={"path": r"C:\Windows\system32"},
    )
    assert up_win.status_code == 400, up_win.text
    assert up_win.json()["detail"]["code"] == "path_escape"

    # tree subpath escape
    t = client.get(f"/api/projects/{pid}/tree", params={"path": "../"})
    assert t.status_code == 400
    assert t.json()["detail"]["code"] == "path_escape"

    assert not outside.exists()


def test_health_ok(client: TestClient):
    assert client.get("/health").status_code == 200
