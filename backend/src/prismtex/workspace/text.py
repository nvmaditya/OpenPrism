"""Content versions, line endings and small TeX-source text helpers.

The API works in LF text; files keep the line endings they had on disk.
A content version is the SHA-256 of the bytes on disk, so it changes
whenever anything (including line endings) changes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

Eol = Literal["lf", "crlf"]


@dataclass(frozen=True)
class FileVersion:
    text: str  # LF-normalized
    version: str  # content_version of the bytes on disk
    eol: Eol


def content_version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_eol(data: bytes) -> Eol:
    """Return "crlf" when CRLF line breaks are the majority, else "lf"."""
    crlf = data.count(b"\r\n")
    lf_only = data.count(b"\n") - crlf
    return "crlf" if crlf > lf_only else "lf"


def decode_text(data: bytes) -> tuple[str, Eol]:
    """Decode strict UTF-8 (raising UnicodeDecodeError) and normalize to LF.

    A BOM is kept as U+FEFF so the file round-trips byte for byte.
    """
    text = data.decode("utf-8")
    return text.replace("\r\n", "\n"), detect_eol(data)


def encode_text(text: str, eol: Eol) -> bytes:
    normalized = text.replace("\r\n", "\n")
    if eol == "crlf":
        normalized = normalized.replace("\n", "\r\n")
    return normalized.encode("utf-8")


def strip_comment(line: str) -> str:
    """The part of a TeX source line before an unescaped `%`."""
    for i, ch in enumerate(line):
        if ch == "%" and (i == 0 or line[i - 1] != "\\"):
            return line[:i]
    return line
