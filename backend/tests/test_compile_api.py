"""Compile routes with an injected fake compiler, and GET /pdf over files on disk."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prismtex.compile import Compiler, CompileResult, Diagnostic, LogSummary
from prismtex.errors import LogNotFound, RootFileInvalid, RootFileMissing
from prismtex.layout import DataLayout
from prismtex.settings import Settings
from prismtex.workspace import Engine, ProjectStore

MakeClient = Callable[..., TestClient]
MakeSettings = Callable[..., Settings]

META: LogSummary = {
    "log_id": "a" * 32,
    "created_at": "2026-11-25T08:00:00.000000+00:00",
    "status": "failed",
    "engine": "pdflatex",
    "root_file": "main.tex",
    "duration_ms": 812,
    "errors": 1,
    "warnings": 0,
}


@dataclass
class FakeCompiler:
    layout: DataLayout
    calls: list[dict[str, Any]] = field(default_factory=list)
    write_pdf: bool = True
    error: Exception | None = None

    def compile(
        self, project_id: str, *, root_file: str | None = None, engine: Engine | None = None
    ) -> CompileResult:
        self.calls.append({"project_id": project_id, "root_file": root_file, "engine": engine})
        if self.error is not None:
            raise self.error
        root = root_file or "main.tex"
        pdf = None
        if self.write_pdf:
            pdf = self.layout.out(project_id) / f"{Path(root).stem}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.5 fake")
        return CompileResult(
            status="ok" if pdf else "failed",
            diagnostics=(
                Diagnostic("sections/x.tex", 3, "error", "undefined_control_sequence", "Bad."),
            ),
            pdf_path=pdf,
            log_text="log text",
            duration_ms=812,
            engine=engine or "pdflatex",
            root_file=root,
            exit_code=0,
            log_id="a" * 32,
        )

    def logs(self, project_id: str) -> list[LogSummary]:
        return [META]

    def get_log(self, project_id: str, log_id: str) -> LogSummary:
        if log_id != META["log_id"]:
            raise LogNotFound(log_id)
        return {**META, "log": "the full log"}

    def is_compiling(self, project_id: str) -> bool:
        return False


@pytest.fixture
def fake(settings: Settings) -> FakeCompiler:
    return FakeCompiler(DataLayout(settings.data_dir))


@pytest.fixture
def api(make_client: MakeClient, fake: FakeCompiler) -> TestClient:
    return make_client(compiler=fake)


def _project(client: TestClient, kind: str = "blank") -> str:
    return client.post("/api/projects", json={"kind": kind}).json()["id"]


def test_compile_returns_the_result(api: TestClient, fake: FakeCompiler) -> None:
    pid = _project(api)
    response = api.post(f"/api/projects/{pid}/compile")
    assert response.status_code == 200
    body = response.json()
    pdf = fake.layout.out(pid) / "main.pdf"
    assert body == {
        "status": "ok",
        "ok": True,
        "diagnostics": [
            {
                "file": "sections/x.tex",
                "line": 3,
                "severity": "error",
                "kind": "undefined_control_sequence",
                "message": "Bad.",
            }
        ],
        "duration_ms": 812,
        "engine": "pdflatex",
        "root_file": "main.tex",
        "log_id": "a" * 32,
        "pdf_url": f"/api/projects/{pid}/pdf?v={pdf.stat().st_mtime_ns}-{'a' * 32}",
    }
    assert fake.calls == [{"project_id": pid, "root_file": None, "engine": None}]


def test_pdf_url_is_unique_per_compile(store: ProjectStore) -> None:
    # CORRECTNESS-6: two PDFs that happen to share an mtime (a coarse-mtime
    # file system) still get different URLs, so the viewer reloads.
    from prismtex.api.compile import _pdf_url
    from prismtex.compile import CompileResult

    info = store.create(kind="blank")
    out = store.layout.out(info.id)
    out.mkdir(parents=True, exist_ok=True)
    pdf = out / "main.pdf"
    pdf.write_bytes(b"%PDF")

    def result(log_id: str) -> CompileResult:
        return CompileResult(
            status="ok",
            diagnostics=(),
            pdf_path=pdf,
            log_text="",
            duration_ms=1,
            engine="pdflatex",
            root_file="main.tex",
            exit_code=0,
            log_id=log_id,
        )

    first = _pdf_url(store, info.id, result("a" * 32))
    second = _pdf_url(store, info.id, result("b" * 32))
    assert first is not None and second is not None
    assert first != second
    assert first.endswith(f"-{'a' * 32}") and second.endswith(f"-{'b' * 32}")


def test_compile_overrides_are_passed_through_and_not_saved(
    api: TestClient, fake: FakeCompiler
) -> None:
    pid = _project(api)
    body = api.post(
        f"/api/projects/{pid}/compile", json={"root_file": "other.tex", "engine": "lualatex"}
    ).json()
    assert fake.calls[-1] == {"project_id": pid, "root_file": "other.tex", "engine": "lualatex"}
    assert body["pdf_url"].startswith(f"/api/projects/{pid}/pdf?v=")
    assert body["pdf_url"].endswith("&root_file=other.tex")
    assert api.get(f"/api/projects/{pid}").json()["root_file"] == "main.tex"
    api.post(f"/api/projects/{pid}/compile", json={})
    assert fake.calls[-1] == {"project_id": pid, "root_file": None, "engine": None}


def test_pdf_url_is_null_without_a_pdf(api: TestClient, fake: FakeCompiler) -> None:
    fake.write_pdf = False
    pid = _project(api)
    body = api.post(f"/api/projects/{pid}/compile").json()
    assert (body["status"], body["ok"], body["pdf_url"]) == ("failed", False, None)


def test_compile_rejects_an_unknown_engine(api: TestClient, fake: FakeCompiler) -> None:
    pid = _project(api)
    response = api.post(f"/api/projects/{pid}/compile", json={"engine": "tex"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert response.json()["detail"]["field"] == "engine"
    assert fake.calls == []


@pytest.mark.parametrize(
    ("error", "code", "extra"),
    [
        (RootFileMissing(), "root_file_missing", {"field": "root_file"}),
        (
            RootFileInvalid("not_found", "There is no file at 'x.tex'."),
            "root_file_invalid",
            {"field": "root_file", "reason": "not_found"},
        ),
    ],
)
def test_compile_errors_use_the_envelope(
    api: TestClient, fake: FakeCompiler, error: Exception, code: str, extra: dict[str, str]
) -> None:
    fake.error = error
    pid = _project(api)
    response = api.post(f"/api/projects/{pid}/compile")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert {k: detail[k] for k in extra} == extra


def test_logs(api: TestClient) -> None:
    pid = _project(api)
    assert api.get(f"/api/projects/{pid}/logs").json() == {"logs": [META]}
    response = api.get(f"/api/projects/{pid}/logs/{'a' * 32}")
    assert response.status_code == 200
    assert response.json() == {**META, "log": "the full log"}
    missing = api.get(f"/api/projects/{pid}/logs/{'b' * 32}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == {
        "code": "log_not_found",
        "message": "This compile log does not exist.",
        "log_id": "b" * 32,
    }


def test_get_pdf_serves_the_root_files_pdf(api: TestClient, fake: FakeCompiler) -> None:
    pid = _project(api)
    url = api.post(f"/api/projects/{pid}/compile").json()["pdf_url"]
    response = api.get(url)
    assert response.status_code == 200
    assert response.content == b"%PDF-1.5 fake"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == 'inline; filename="main.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"


def test_get_pdf_of_another_root_file(api: TestClient) -> None:
    pid = _project(api)
    url = api.post(f"/api/projects/{pid}/compile", json={"root_file": "sub/other.tex"}).json()[
        "pdf_url"
    ]
    response = api.get(url)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'inline; filename="other.pdf"'


@pytest.mark.parametrize("query", ["", "?root_file=../../etc/passwd.tex", "?root_file=x.txt"])
def test_get_pdf_without_a_pdf_is_404(api: TestClient, query: str) -> None:
    pid = _project(api)
    response = api.get(f"/api/projects/{pid}/pdf{query}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "pdf_not_found"


def test_get_pdf_without_a_root_file_is_404(
    api: TestClient, fake: FakeCompiler, store: ProjectStore
) -> None:
    pid = _project(api)
    api.post(f"/api/projects/{pid}/compile")
    api.patch(f"/api/projects/{pid}", json={"root_file": None})
    response = api.get(f"/api/projects/{pid}/pdf")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "pdf_not_found"


def test_get_pdf_of_an_unknown_project_is_404(api: TestClient) -> None:
    response = api.get(f"/api/projects/{'0' * 32}/pdf")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "project_not_found"


def test_routes_are_in_the_openapi_schema(api: TestClient) -> None:
    paths = api.get("/api/openapi.json").json()["paths"]
    for path in ("compile", "pdf", "logs", "logs/{log_id}"):
        assert f"/api/projects/{{project_id}}/{path}" in paths


def test_default_app_uses_the_real_compiler(
    make_client: MakeClient,
    make_settings: MakeSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    client = make_client(settings=make_settings(compile_timeout_s=7.5))
    assert isinstance(client.app, FastAPI)
    compiler = client.app.state.compiler
    assert isinstance(compiler, Compiler)
    assert compiler.timeout_s == 7.5
    pid = _project(client)
    body = client.post(f"/api/projects/{pid}/compile").json()
    assert body["status"] == "unavailable"
    assert [d["kind"] for d in body["diagnostics"]] == ["tex_missing"]
    assert body["pdf_url"] is None
    [meta] = client.get(f"/api/projects/{pid}/logs").json()["logs"]
    assert meta["log_id"] == body["log_id"]
    assert (
        "latexmk was not found"
        in client.get(f"/api/projects/{pid}/logs/{body['log_id']}").json()["log"]
    )


def test_real_compiler_errors_over_http(
    make_client: MakeClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    client = make_client()
    unknown = client.post(f"/api/projects/{'0' * 32}/compile")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "project_not_found"
    pid = _project(client)
    bad = client.post(f"/api/projects/{pid}/compile", json={"root_file": "../x.tex"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "root_file_invalid"
    client.patch(f"/api/projects/{pid}", json={"root_file": None})
    missing = client.post(f"/api/projects/{pid}/compile")
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "root_file_missing"
    no_log = client.get(f"/api/projects/{pid}/logs/{'c' * 32}")
    assert no_log.status_code == 404
    assert no_log.json()["detail"]["code"] == "log_not_found"
