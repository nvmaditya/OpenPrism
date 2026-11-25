"""Project files: list, tree, read, write with ETag/If-Match, upload, rename, delete."""

from __future__ import annotations

import base64
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Header, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from prismtex.api.deps import SettingsDep, StoreDep
from prismtex.errors import InvalidRequest, PayloadTooLarge
from prismtex.workspace import Eol, check_relative_path, decode_text

router = APIRouter(prefix="/api/projects/{project_id}", tags=["files"])

FileKind = Literal["source", "asset"]


class FileOut(BaseModel):
    path: str
    size: int
    kind: FileKind


class FileList(BaseModel):
    files: list[FileOut]


class TreeNode(BaseModel):
    name: str
    path: str
    type: Literal["dir", "file"]
    size: int | None = None
    kind: FileKind | None = None
    children: list[TreeNode] | None = None


class Tree(BaseModel):
    tree: TreeNode


class TextFile(BaseModel):
    path: str
    encoding: Literal["utf-8"]
    content: str
    version: str
    eol: Eol


class BinaryFile(BaseModel):
    path: str
    encoding: Literal["base64"]
    content: str
    version: str


class WriteBody(BaseModel):
    content: str


class Written(BaseModel):
    path: str
    version: str
    eol: Eol


class RenameBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    src: str = Field(alias="from")
    dst: str = Field(alias="to")


class Renamed(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    src: str = Field(alias="from")
    dst: str = Field(alias="to")


class DeleteBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)


class DeleteError(BaseModel):
    path: str
    code: str
    message: str


class Deleted(BaseModel):
    deleted: list[str]
    errors: list[DeleteError]


def _etag(version: str) -> str:
    return f'"{version}"'


def _parse_if_match(value: str | None) -> str | None:
    """The version in an If-Match header; quotes and a weak `W/` prefix are optional."""
    if value is None:
        return None
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip().strip('"')
    # `*` means "any current version", which is what no header means here.
    return None if value in ("", "*") else value


@router.get("/files")
def list_files(project_id: str, store: StoreDep) -> FileList:
    entries = store.list_files(project_id)
    return FileList(files=[FileOut.model_validate(entry.to_dict()) for entry in entries])


@router.get("/tree", response_model_exclude_none=True)
def get_tree(project_id: str, store: StoreDep, path: str = "") -> Tree:
    return Tree(tree=TreeNode.model_validate(store.tree(project_id, path)))


@router.post("/upload", status_code=201)
def upload_file(
    project_id: str,
    store: StoreDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    folder: Annotated[str, Form(max_length=1000)] = "",
) -> FileOut:
    if not file.filename:
        raise InvalidRequest("The upload has no file name.", field="file")
    # Read one byte past the limit so an oversized file is caught without
    # holding all of it in memory.
    data = file.file.read(store.max_upload_bytes + 1)
    if len(data) > store.max_upload_bytes:
        raise PayloadTooLarge(settings.max_upload_mb)
    entry = store.upload(project_id, filename=file.filename, data=data, folder=folder)
    return FileOut.model_validate(entry.to_dict())


@router.post("/files/rename")
def rename_file(project_id: str, body: RenameBody, store: StoreDep) -> Renamed:
    store.rename(project_id, body.src, body.dst)
    return Renamed.model_validate(
        {"from": check_relative_path(body.src), "to": check_relative_path(body.dst)}
    )


@router.post("/files/delete")
def delete_files(project_id: str, body: DeleteBody, store: StoreDep) -> Deleted:
    return Deleted.model_validate(store.delete_files(project_id, body.paths))


@router.get("/files/{file_path:path}")
def read_file(
    project_id: str, file_path: str, store: StoreDep, response: Response
) -> TextFile | BinaryFile:
    data, version = store.read_bytes(project_id, file_path)
    path = check_relative_path(file_path)
    response.headers["ETag"] = _etag(version)
    try:
        text, eol = decode_text(data)
    except UnicodeDecodeError:
        content = base64.b64encode(data).decode("ascii")
        return BinaryFile(path=path, encoding="base64", content=content, version=version)
    return TextFile(path=path, encoding="utf-8", content=text, version=version, eol=eol)


@router.put("/files/{file_path:path}")
def write_file(
    project_id: str,
    file_path: str,
    body: WriteBody,
    store: StoreDep,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> Written:
    written = store.write(project_id, file_path, body.content, _parse_if_match(if_match))
    response.headers["ETag"] = _etag(written.version)
    return Written(path=check_relative_path(file_path), version=written.version, eol=written.eol)


@router.delete("/files/{file_path:path}", status_code=204)
def delete_file(project_id: str, file_path: str, store: StoreDep) -> Response:
    store.delete_file(project_id, file_path)
    return Response(status_code=204)
