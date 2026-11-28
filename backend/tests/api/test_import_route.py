"""POST /api/projects/import with a source, and its `import` Checkpoint (agent design §15 T24)."""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import zipfile
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prismtex.history import Checkpoint, GitShadowHistory, HistoryError, NullHistory

DOC = b"\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"
PRISM_EXPORT = {
    "My Paper/paper.tex": DOC,
    "My Paper/sections/intro.tex": b"\\section{Intro}\n",
    "My Paper/conversations/c1.json": b"{}",
}


def zip_bytes(files: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def post_import(client: TestClient, files: Mapping[str, bytes], **form: str) -> Any:
    return client.post(
        "/api/projects/import",
        files={"file": ("export.zip", zip_bytes(files), "application/zip")},
        data=form,
    )


def app_of(client: TestClient) -> FastAPI:
    assert isinstance(client.app, FastAPI)
    return client.app


def file_paths(client: TestClient, project_id: str) -> list[str]:
    return [f["path"] for f in client.get(f"/api/projects/{project_id}/files").json()["files"]]


def test_import_route_accepts_source_takes_an_import_checkpoint_and_returns_root_file(
    client: TestClient,
) -> None:
    history = app_of(client).state.history
    assert isinstance(history, GitShadowHistory)
    response = post_import(client, PRISM_EXPORT, name="My Paper", source="prism")
    assert response.status_code == 201
    project = response.json()
    assert (project["kind"], project["name"], project["root_file"]) == (
        "import",
        "My Paper",
        "paper.tex",
    )
    pid = project["id"]
    assert file_paths(client, pid) == ["paper.tex", "sections/intro.tex"]
    [cp] = asyncio.run(history.list(pid))
    assert (cp.reason, cp.message) == ("import", "Import My Paper")
    assert cp.files_changed == 2


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ({}, ["paper.tex", "sections/intro.tex"]),
        ({"source": "auto"}, ["paper.tex", "sections/intro.tex"]),
        ({"source": "overleaf"}, ["conversations/c1.json", "paper.tex", "sections/intro.tex"]),
    ],
)
def test_source_defaults_to_auto_and_overleaf_keeps_conversations(
    client: TestClient, form: dict[str, str], expected: list[str]
) -> None:
    response = post_import(client, PRISM_EXPORT, **form)
    assert response.status_code == 201
    assert response.json()["root_file"] == "paper.tex"
    assert file_paths(client, response.json()["id"]) == expected


def test_an_unknown_source_is_a_422_and_creates_nothing(client: TestClient) -> None:
    response = post_import(client, PRISM_EXPORT, source="dropbox")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert (detail["code"], detail["field"]) == ("invalid_request", "source")
    assert client.get("/api/projects").json() == {"projects": []}


def test_import_without_git_still_imports(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_which = shutil.which

    def which(cmd: str, *args: Any, **kwargs: Any) -> str | None:
        return None if cmd == "git" else real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    client = make_client()
    assert isinstance(app_of(client).state.history, NullHistory)
    response = post_import(client, PRISM_EXPORT, name="My Paper")
    assert response.status_code == 201
    project = response.json()
    assert project["root_file"] == "paper.tex"
    assert file_paths(client, project["id"]) == ["paper.tex", "sections/intro.tex"]


class FailingHistory(NullHistory):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def checkpoint(
        self,
        project_id: str,
        *,
        reason: Any,
        message: str,
        trailers: Mapping[str, str] | None = None,
    ) -> Checkpoint | None:
        self.calls.append((project_id, reason, message))
        raise HistoryError("git failed")


def test_a_failed_import_checkpoint_still_returns_the_project(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    failing = FailingHistory()
    app_of(client).state.history = failing
    with caplog.at_level(logging.WARNING, logger="prismtex"):
        response = post_import(client, PRISM_EXPORT, name="My Paper")
    assert response.status_code == 201
    pid = response.json()["id"]
    assert failing.calls == [(pid, "import", "Import My Paper")]
    assert client.get(f"/api/projects/{pid}").status_code == 200
    assert file_paths(client, pid) == ["paper.tex", "sections/intro.tex"]
    assert any(pid in record.getMessage() for record in caplog.records)


def test_a_rejected_archive_takes_no_checkpoint(client: TestClient) -> None:
    failing = FailingHistory()
    app_of(client).state.history = failing
    response = post_import(client, {"../evil.tex": b"x"}, source="prism")
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "zip_slip"
    assert failing.calls == []
