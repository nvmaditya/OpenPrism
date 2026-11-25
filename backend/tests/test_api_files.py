from __future__ import annotations

import base64
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from prismtex.settings import Settings
from prismtex.workspace import content_version


@pytest.fixture
def pid(client: TestClient) -> str:
    return client.post("/api/projects", json={"kind": "paper"}).json()["id"]


def files_url(pid: str, path: str = "") -> str:
    return f"/api/projects/{pid}/files" + (f"/{path}" if path else "")


def test_list_files(client: TestClient, pid: str) -> None:
    files = client.get(files_url(pid)).json()["files"]
    by_path = {f["path"]: f for f in files}
    assert by_path["main.tex"]["kind"] == "source"
    assert by_path["figures/example-figure.png"]["kind"] == "asset"
    assert all(set(f) == {"path", "size", "kind"} for f in files)


def test_tree(client: TestClient, pid: str) -> None:
    tree = client.get(f"/api/projects/{pid}/tree").json()["tree"]
    assert (tree["name"], tree["path"], tree["type"]) == ("Example paper", "", "dir")
    figures = tree["children"][0]
    assert set(figures) == {"name", "path", "type", "children"}
    png = figures["children"][0]
    assert set(png) == {"name", "path", "type", "size", "kind"}
    sub = client.get(f"/api/projects/{pid}/tree", params={"path": "sections"}).json()["tree"]
    assert sub["path"] == "sections"
    bad = client.get(f"/api/projects/{pid}/tree", params={"path": "../.."})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "path_denied"


def test_read_text_with_etag(client: TestClient, pid: str) -> None:
    response = client.get(files_url(pid, "sections/method.tex"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"path", "encoding", "content", "version", "eol"}
    assert (body["path"], body["encoding"], body["eol"]) == ("sections/method.tex", "utf-8", "lf")
    assert "\\section{Method}" in body["content"]
    assert response.headers["etag"] == f'"{body["version"]}"'


def test_read_binary_as_base64(client: TestClient, pid: str) -> None:
    response = client.get(files_url(pid, "figures/example-figure.png"))
    body = response.json()
    assert set(body) == {"path", "encoding", "content", "version"}
    data = base64.b64decode(body["content"])
    assert data.startswith(b"\x89PNG")
    assert body["version"] == content_version(data)
    assert response.headers["etag"] == f'"{body["version"]}"'


def test_read_errors(client: TestClient, pid: str) -> None:
    missing = client.get(files_url(pid, "missing.tex"))
    assert missing.status_code == 404
    assert missing.json()["detail"] == {
        "code": "file_not_found",
        "message": "There is no file at 'missing.tex'.",
        "path": "missing.tex",
    }
    denied = client.get(files_url(pid, "con.tex"))
    assert denied.status_code == 400
    assert denied.json()["detail"]["reason"] == "device_name"
    internal = client.get(files_url(pid, ".git/config"))
    assert internal.json()["detail"]["reason"] == "internal"
    unknown = client.get(files_url("0" * 32, "main.tex"))
    assert unknown.json()["detail"]["code"] == "project_not_found"


def test_save_with_if_match(client: TestClient, pid: str) -> None:
    loaded = client.get(files_url(pid, "main.tex"))
    version = loaded.json()["version"]

    saved = client.put(
        files_url(pid, "main.tex"),
        json={"content": "first edit\n"},
        headers={"If-Match": f'"{version}"'},
    )
    assert saved.status_code == 200
    new_version = saved.json()["version"]
    assert saved.json() == {"path": "main.tex", "version": new_version, "eol": "lf"}
    assert saved.headers["etag"] == f'"{new_version}"'

    # Saving again with the old version conflicts and leaves the file alone.
    stale = client.put(
        files_url(pid, "main.tex"),
        json={"content": "lost edit\n"},
        headers={"If-Match": f'"{version}"'},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "message": "'main.tex' changed on disk since it was read.",
        "path": "main.tex",
        "current_version": new_version,
    }
    assert client.get(files_url(pid, "main.tex")).json()["content"] == "first edit\n"

    # Overwrite: save with the conflict's current_version; quotes are optional.
    forced = client.put(
        files_url(pid, "main.tex"),
        json={"content": "forced\n"},
        headers={"If-Match": new_version},
    )
    assert forced.status_code == 200


def test_save_without_if_match_is_unconditional(client: TestClient, pid: str) -> None:
    response = client.put(files_url(pid, "notes/new.tex"), json={"content": "a\r\nb\n"})
    assert response.status_code == 200
    assert response.json()["eol"] == "lf"
    assert client.get(files_url(pid, "notes/new.tex")).json()["content"] == "a\nb\n"


def test_if_match_on_missing_file_conflicts(client: TestClient, pid: str) -> None:
    response = client.put(
        files_url(pid, "gone.tex"), json={"content": "x"}, headers={"If-Match": '"abc"'}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["current_version"] is None


def test_crlf_file_stays_crlf_through_the_api(client: TestClient, pid: str) -> None:
    client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("win.tex", b"a\r\nb\r\n", "text/plain")},
    )
    loaded = client.get(files_url(pid, "win.tex")).json()
    assert (loaded["content"], loaded["eol"]) == ("a\nb\n", "crlf")
    saved = client.put(
        files_url(pid, "win.tex"),
        json={"content": loaded["content"] + "c\n"},
        headers={"If-Match": loaded["version"]},
    ).json()
    assert saved["eol"] == "crlf"
    assert saved["version"] == content_version(b"a\r\nb\r\nc\r\n")


def test_upload(client: TestClient, pid: str) -> None:
    response = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("plot.png", b"\x89PNG data", "image/png")},
        data={"folder": "figures"},
    )
    assert response.status_code == 201
    assert response.json() == {"path": "figures/plot.png", "size": 9, "kind": "asset"}
    root = client.post(
        f"/api/projects/{pid}/upload", files={"file": ("refs.bib", b"@book{}", "text/plain")}
    )
    assert root.json()["path"] == "refs.bib"
    bad = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("x.png", b"x", "image/png")},
        data={"folder": "../outside"},
    )
    assert bad.status_code == 400


def test_upload_over_limit_is_413(
    make_client: Callable[..., TestClient], make_settings: Callable[..., Settings]
) -> None:
    client = make_client(settings=make_settings(max_upload_mb=1))
    pid = client.post("/api/projects", json={}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/upload",
        files={"file": ("big.bin", b"x" * (2**20 + 1), "application/octet-stream")},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["limit_mb"] == 1
    files = client.get(files_url(pid)).json()["files"]
    assert [f["path"] for f in files] == ["main.tex"]


def test_rename(client: TestClient, pid: str) -> None:
    response = client.post(
        f"/api/projects/{pid}/files/rename",
        json={"from": "./sections/method.tex", "to": "sections/approach.tex"},
    )
    assert response.status_code == 200
    assert response.json() == {"from": "sections/method.tex", "to": "sections/approach.tex"}
    clash = client.post(
        f"/api/projects/{pid}/files/rename",
        json={"from": "sections/approach.tex", "to": "main.tex"},
    )
    assert clash.status_code == 409
    assert clash.json()["detail"] == {
        "code": "file_exists",
        "message": "Something already exists at 'main.tex'.",
        "path": "main.tex",
    }


def test_delete_one_and_many(client: TestClient, pid: str) -> None:
    response = client.delete(files_url(pid, "sections/results.tex"))
    assert response.status_code == 204
    assert client.get(files_url(pid, "sections/results.tex")).status_code == 404

    batch = client.post(
        f"/api/projects/{pid}/files/delete",
        json={"paths": ["references.bib", "missing.tex"]},
    )
    assert batch.status_code == 200
    body = batch.json()
    assert body["deleted"] == ["references.bib"]
    assert body["errors"] == [
        {
            "path": "missing.tex",
            "code": "file_not_found",
            "message": "There is no file at 'missing.tex'.",
        }
    ]
    empty = client.post(f"/api/projects/{pid}/files/delete", json={"paths": []})
    assert empty.status_code == 422
    too_many = client.post(f"/api/projects/{pid}/files/delete", json={"paths": ["a"] * 1001})
    assert too_many.status_code == 422
