"""Path policy: which project-relative paths the file API accepts.

The rules are pure string checks, the same on every OS, so a project that
works on Linux also unpacks and compiles on Windows (no drive letters,
device names, alternate data streams or trailing dots).
"""

from __future__ import annotations

import re
from pathlib import Path

from prismtex.errors import PathPolicyError, RootFileInvalid

__all__ = [
    "INTERNAL_DIRS",
    "PathPolicyError",
    "check_relative_path",
    "check_root_file",
    "is_internal",
    "resolve_in",
]

# A file-system name limit, measured in UTF-8 bytes. Linux NAME_MAX is 255
# bytes; NTFS and HFS+ cap at 255 UTF-16 units and APFS at 255 UTF-8 bytes.
# A UTF-8 byte count is never smaller than a UTF-16 unit count, so one check
# in bytes covers every one of them.
MAX_SEGMENT = 255
MAX_PATH = 240
INTERNAL_DIRS = frozenset({".git", ".prism"})

_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED_CHARS = frozenset('<>:"|?*')
# Windows treats the superscript digits as device numbers too (COM¹, LPT²), and
# CONIN$/CONOUT$ are reserved alongside CON, PRN and the rest.
_DEVICE = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])$", re.IGNORECASE)
_ROOT_FILE_FORBIDDEN = frozenset("%#$&~^{}\\")

_MESSAGES = {
    "empty": "The path is empty.",
    "control_char": "The path contains a control character.",
    "unc": "Network (UNC) paths are not allowed.",
    "absolute": "Absolute paths are not allowed; use a path inside the project.",
    "drive": "Paths with a drive letter are not allowed; use a path inside the project.",
    "drive_relative": "Paths with a drive letter are not allowed; use a path inside the project.",
    "parent": "Paths may not contain '..'.",
    "reserved_char": 'Paths may not contain any of < > : " | ? *.',
    "device_name": "That name is reserved by Windows (CON, PRN, AUX, NUL, COM1, LPT1, ...).",
    "trailing_dot_or_space": "File and folder names may not end in a dot or a space.",
    "segment_too_long": f"File and folder names may be at most {MAX_SEGMENT} bytes (UTF-8).",
    "too_long": f"Paths may be at most {MAX_PATH} characters.",
    "internal": "This folder is reserved and cannot be read or changed.",
    "outside_root": "The path leads outside the project folder.",
}


def _deny(path: str, reason: str) -> PathPolicyError:
    return PathPolicyError(path, reason, _MESSAGES[reason])


def check_relative_path(path: str) -> str:
    """Return `path` as a normalized POSIX relative path, or raise PathPolicyError."""
    if any(ord(ch) < 32 for ch in path):
        raise _deny(path, "control_char")
    raw = path.replace("\\", "/")
    if not raw:
        raise _deny(path, "empty")
    if raw.startswith("//"):
        raise _deny(path, "unc")
    if raw.startswith("/"):
        raise _deny(path, "absolute")
    if _DRIVE.match(raw):
        raise _deny(path, "drive" if raw[2:3] == "/" else "drive_relative")
    segments = [seg for seg in raw.split("/") if seg not in ("", ".")]
    if not segments:
        raise _deny(path, "empty")
    for seg in segments:
        if seg == "..":
            raise _deny(path, "parent")
        if any(ch in _RESERVED_CHARS for ch in seg):
            raise _deny(path, "reserved_char")
        # Windows matches a device name against the part before the first dot
        # with trailing spaces stripped, so "con .tex" and "nul.bib" both resolve
        # to a device. Mirror that, rather than only the exact "con" case.
        if _DEVICE.match(seg.partition(".")[0].rstrip(" ")):
            raise _deny(path, "device_name")
        if seg.endswith((".", " ")):
            raise _deny(path, "trailing_dot_or_space")
        if len(seg.encode("utf-8")) > MAX_SEGMENT:
            raise _deny(path, "segment_too_long")
    normalized = "/".join(segments)
    if len(normalized) > MAX_PATH:
        raise _deny(path, "too_long")
    return normalized


def is_internal(rel: str) -> bool:
    """True when a normalized path lies in `.git/` or `.prism/` at the project root."""
    return rel.split("/", 1)[0].lower() in INTERNAL_DIRS


def resolve_in(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, following symlinks; refuse anything that escapes."""
    normalized = check_relative_path(rel)
    base = root.resolve()
    candidate = (base / normalized).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise _deny(rel, "outside_root")
    return candidate


def check_root_file(root: Path, root_file: str) -> str:
    """Validate a root file for compiling: an existing `.tex` file inside `root`.

    The name reaches latexmk's argv and TeX's input, so it is held to a
    stricter rule than other paths. Returns the normalized path.
    """
    if any(ch in _ROOT_FILE_FORBIDDEN for ch in root_file):
        raise RootFileInvalid(
            "bad_char", "The root file name may not contain any of % # $ & ~ ^ { } \\."
        )
    if any(ch.isspace() and ch != " " for ch in root_file):
        raise RootFileInvalid("bad_char", "The root file name may not contain tabs or newlines.")
    try:
        normalized = check_relative_path(root_file)
    except PathPolicyError as exc:
        raise RootFileInvalid("path_denied", exc.message) from exc
    if normalized.startswith("-"):
        raise RootFileInvalid("leading_dash", "The root file name may not start with '-'.")
    if not normalized.lower().endswith(".tex"):
        raise RootFileInvalid("not_tex", "The root file must be a .tex file.")
    if is_internal(normalized):
        raise RootFileInvalid("path_denied", _MESSAGES["internal"])
    try:
        target = resolve_in(root, normalized)
    except PathPolicyError as exc:
        raise RootFileInvalid("path_denied", exc.message) from exc
    if not target.is_file():
        raise RootFileInvalid("not_found", f"There is no file at '{normalized}'.")
    return normalized
