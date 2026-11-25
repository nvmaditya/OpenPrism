"""Projects and their files, behind the path policy (revamp §6.2)."""

from prismtex.errors import PathPolicyError
from prismtex.workspace.paths import (
    check_relative_path,
    check_root_file,
    is_internal,
    resolve_in,
)
from prismtex.workspace.store import (
    ENGINES,
    SOURCE_EXTENSIONS,
    UNSET,
    Engine,
    FileEntry,
    ProjectInfo,
    ProjectKind,
    ProjectSettings,
    ProjectStore,
    TemplateKind,
    Unset,
    file_kind,
    remove_stale_temp_files,
)
from prismtex.workspace.text import (
    Eol,
    FileVersion,
    content_version,
    decode_text,
    detect_eol,
    encode_text,
)
from prismtex.workspace.zipimport import ValidatedZip, detect_root_file

__all__ = [
    "ENGINES",
    "SOURCE_EXTENSIONS",
    "UNSET",
    "Engine",
    "Eol",
    "FileEntry",
    "FileVersion",
    "PathPolicyError",
    "ProjectInfo",
    "ProjectKind",
    "ProjectSettings",
    "ProjectStore",
    "TemplateKind",
    "Unset",
    "ValidatedZip",
    "check_relative_path",
    "check_root_file",
    "content_version",
    "decode_text",
    "detect_eol",
    "detect_root_file",
    "encode_text",
    "file_kind",
    "is_internal",
    "remove_stale_temp_files",
    "resolve_in",
]
