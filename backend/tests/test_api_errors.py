"""Every error response uses the envelope {"detail": {"code", "message", ...}}."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prismtex import errors
from prismtex.errors import AppError

DOMAIN_ERRORS: list[tuple[AppError, int, str, dict[str, object]]] = [
    (errors.ProjectNotFound("p1"), 404, "project_not_found", {"project_id": "p1"}),
    (errors.FileNotFound("a.tex"), 404, "file_not_found", {"path": "a.tex"}),
    (
        errors.PathPolicyError("../x", "parent", "no"),
        400,
        "path_denied",
        {"path": "../x", "reason": "parent"},
    ),
    (
        errors.VersionConflict("a.tex", "v2"),
        409,
        "version_conflict",
        {"path": "a.tex", "current_version": "v2"},
    ),
    (
        errors.VersionConflict("a.tex", None),
        409,
        "version_conflict",
        {"path": "a.tex", "current_version": None},
    ),
    (errors.FileExists("a.tex"), 409, "file_exists", {"path": "a.tex"}),
    (errors.NotText("a.png"), 415, "not_text", {"path": "a.png"}),
    (errors.InvalidRequest("bad", field="name"), 422, "invalid_request", {"field": "name"}),
    (errors.RootFileMissing(), 422, "root_file_missing", {"field": "root_file"}),
    (
        errors.RootFileInvalid("not_tex", "no"),
        422,
        "root_file_invalid",
        {"field": "root_file", "reason": "not_tex"},
    ),
    (errors.ImportRejected("zip_slip", "no"), 400, "import_rejected", {"reason": "zip_slip"}),
    (errors.PayloadTooLarge(50), 413, "payload_too_large", {"limit_mb": 50}),
    (errors.FileBusy("a.tex"), 409, "file_busy", {"path": "a.tex"}),
    (errors.ProjectBusy("p1"), 409, "project_busy", {"project_id": "p1"}),
    (errors.UnsafeProjectFile("a.tex"), 409, "unsafe_project_file", {"path": "a.tex"}),
    (errors.LogNotFound("l1"), 404, "log_not_found", {"log_id": "l1"}),
    (errors.PdfNotFound(), 404, "pdf_not_found", {}),
]


def _raiser(error: Exception) -> Callable[[], None]:
    def route() -> None:
        raise error

    return route


@pytest.mark.parametrize(("error", "status", "code", "extra"), DOMAIN_ERRORS)
def test_domain_errors(
    app: FastAPI,
    client: TestClient,
    error: AppError,
    status: int,
    code: str,
    extra: dict[str, object],
) -> None:
    app.add_api_route("/api/test/raise", _raiser(error))
    response = client.get("/api/test/raise")
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail.pop("code") == code
    assert isinstance(detail.pop("message"), str) and error.message
    assert detail == extra
    assert error.to_body() == response.json()


def test_every_app_error_is_covered() -> None:
    covered = {type(error) for error, *_ in DOMAIN_ERRORS}
    defined = {
        cls
        for cls in vars(errors).values()
        if isinstance(cls, type) and issubclass(cls, AppError) and cls is not AppError
    }
    assert defined == covered


def test_unknown_route_is_404(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "not_found", "message": "There is nothing at this address."}
    }


def test_wrong_method_is_405(client: TestClient) -> None:
    response = client.put("/api/health")
    assert response.status_code == 405
    assert response.json()["detail"]["code"] == "method_not_allowed"
    assert "GET" in response.headers["allow"]


def test_validation_error_is_422(client: TestClient) -> None:
    response = client.post("/api/projects", json={"kind": "novel"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert (detail["code"], detail["field"]) == ("invalid_request", "kind")
    assert isinstance(detail["message"], str)


def test_validation_error_nested_field(client: TestClient) -> None:
    project = client.post("/api/projects", json={}).json()
    response = client.post(f"/api/projects/{project['id']}/files/delete", json={"paths": [1]})
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "paths.0"


def test_malformed_json_is_422(client: TestClient) -> None:
    response = client.post(
        "/api/projects", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"


def test_unhandled_error_is_500_without_details(
    make_client: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(raise_server_exceptions=False)
    app = client.app
    assert isinstance(app, FastAPI)
    app.add_api_route("/api/test/boom", _raiser(RuntimeError("secret internals")))
    response = client.get("/api/test/boom")
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Something went wrong on the server."}
    }
    assert "secret internals" not in response.text
    assert "secret internals" in caplog.text
