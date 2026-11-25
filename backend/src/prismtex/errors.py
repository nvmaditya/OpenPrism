"""Domain errors and the one error envelope every response uses.

Every error body is `{"detail": {"code": ..., "message": ..., **extra}}`,
whether it comes from a route, validation, routing or the security middleware.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("prismtex")


class AppError(Exception):
    """A failure the client can act on; carries an HTTP status and a stable code."""

    status: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(message)
        self.message = message
        self.extra: dict[str, object] = extra

    def to_body(self) -> dict[str, Any]:
        return envelope(self.code, self.message, **self.extra)


class ProjectNotFound(AppError):
    status = 404
    code = "project_not_found"

    def __init__(self, project_id: str) -> None:
        super().__init__("This project does not exist.", project_id=project_id)


class FileNotFound(AppError):
    status = 404
    code = "file_not_found"

    def __init__(self, path: str) -> None:
        super().__init__(f"There is no file at '{path}'.", path=path)


class PathPolicyError(AppError):
    status = 400
    code = "path_denied"

    def __init__(self, path: str, reason: str, message: str) -> None:
        super().__init__(message, path=path, reason=reason)
        self.reason = reason


class VersionConflict(AppError):
    status = 409
    code = "version_conflict"

    def __init__(self, path: str, current_version: str | None) -> None:
        message = (
            f"'{path}' changed on disk since it was read."
            if current_version is not None
            else f"'{path}' no longer exists on disk."
        )
        super().__init__(message, path=path, current_version=current_version)
        self.current_version = current_version


class FileExists(AppError):
    status = 409
    code = "file_exists"

    def __init__(self, path: str) -> None:
        super().__init__(f"Something already exists at '{path}'.", path=path)


class NotText(AppError):
    status = 415
    code = "not_text"

    def __init__(self, path: str) -> None:
        super().__init__(f"'{path}' is not a UTF-8 text file.", path=path)


class InvalidRequest(AppError):
    status = 422
    code = "invalid_request"

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message, field=field)


class RootFileMissing(AppError):
    status = 422
    code = "root_file_missing"

    def __init__(self) -> None:
        super().__init__(
            "This project has no root file. Choose one in Project Info.", field="root_file"
        )


class RootFileInvalid(AppError):
    status = 422
    code = "root_file_invalid"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, field="root_file", reason=reason)
        self.reason = reason


class ImportRejected(AppError):
    status = 400
    code = "import_rejected"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, reason=reason)
        self.reason = reason


class PayloadTooLarge(AppError):
    status = 413
    code = "payload_too_large"

    def __init__(self, limit_mb: float) -> None:
        shown = int(limit_mb) if float(limit_mb).is_integer() else limit_mb
        super().__init__(f"This is larger than the {shown} MB limit.", limit_mb=shown)


class LogNotFound(AppError):
    status = 404
    code = "log_not_found"

    def __init__(self, log_id: str) -> None:
        super().__init__("This compile log does not exist.", log_id=log_id)


class PdfNotFound(AppError):
    status = 404
    code = "pdf_not_found"

    def __init__(self) -> None:
        super().__init__("There is no compiled PDF yet. Compile the project first.")


def envelope(code: str, message: str, **extra: object) -> dict[str, Any]:
    return {"detail": {"code": code, "message": message, **extra}}


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    return JSONResponse(envelope(code, message, **extra), status_code=status, headers=headers)


_LOCATION_ROOTS = frozenset({"body", "query", "path", "header", "cookie"})


def _validation_field(loc: tuple[int | str, ...]) -> str:
    parts = list(loc)
    if len(parts) > 1 and parts[0] in _LOCATION_ROOTS:
        parts = parts[1:]
    return ".".join(str(part) for part in parts)


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(exc.to_body(), status_code=exc.status)


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    if not errors:
        return error_response(422, "invalid_request", "The request is not valid.", field="")
    first = errors[0]
    field = _validation_field(tuple(first.get("loc", ())))
    message = str(first.get("msg", "Invalid value"))
    shown = f"{field}: {message}" if field else message
    return error_response(422, "invalid_request", shown, field=field)


async def _http_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    headers = dict(exc.headers) if exc.headers else None
    if exc.status_code == 404:
        return error_response(
            404, "not_found", "There is nothing at this address.", headers=headers
        )
    if exc.status_code == 405:
        return error_response(
            405, "method_not_allowed", "This method is not allowed here.", headers=headers
        )
    message = exc.detail if isinstance(exc.detail, str) else "The request failed."
    return error_response(exc.status_code, "http_error", message, headers=headers)


async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
    # The details stay in the server log; the client gets a plain message.
    log.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, "internal_error", "Something went wrong on the server.")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _internal_error)
