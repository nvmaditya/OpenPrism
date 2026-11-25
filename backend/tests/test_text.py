from __future__ import annotations

import hashlib

import pytest

from prismtex.workspace import content_version, decode_text, detect_eol, encode_text
from prismtex.workspace.text import strip_comment


def test_content_version_is_sha256_hex() -> None:
    data = b"\\documentclass{article}\n"
    version = content_version(data)
    assert version == hashlib.sha256(data).hexdigest()
    assert len(version) == 64
    assert content_version(b"a\n") != content_version(b"a\r\n")


@pytest.mark.parametrize(
    ("data", "eol"),
    [
        (b"", "lf"),
        (b"one line", "lf"),
        (b"a\nb\n", "lf"),
        (b"a\r\nb\r\n", "crlf"),
        (b"a\r\nb\r\nc\n", "crlf"),
        (b"a\r\nb\nc\n", "lf"),
        (b"a\r\nb\n", "lf"),  # a tie is LF
    ],
)
def test_detect_eol(data: bytes, eol: str) -> None:
    assert detect_eol(data) == eol


def test_decode_normalizes_to_lf() -> None:
    assert decode_text(b"a\r\nb\r\n") == ("a\nb\n", "crlf")
    assert decode_text(b"a\nb\n") == ("a\nb\n", "lf")


def test_decode_rejects_invalid_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        decode_text(b"\xff\xfe\x00")


@pytest.mark.parametrize(
    "data",
    [b"a\nb\n", b"a\r\nb\r\n", b"\xef\xbb\xbfBOM\r\nfile\r\n", "r\u00e9sum\u00e9\n".encode()],
)
def test_round_trip_is_byte_identical(data: bytes) -> None:
    text, eol = decode_text(data)
    assert encode_text(text, eol) == data


def test_bom_stays_in_text() -> None:
    text, _ = decode_text(b"\xef\xbb\xbfhello")
    assert text == "\ufeffhello"


def test_encode_applies_eol_and_normalizes_input() -> None:
    assert encode_text("a\nb\r\nc", "crlf") == b"a\r\nb\r\nc"
    assert encode_text("a\r\nb\n", "lf") == b"a\nb\n"


def test_mixed_line_endings_normalize_to_majority() -> None:
    text, eol = decode_text(b"a\r\nb\r\nc\n")
    assert encode_text(text, eol) == b"a\r\nb\r\nc\r\n"


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("text % comment", "text "),
        ("% all comment", ""),
        ("50\\% off % note", "50\\% off "),
        ("no comment", "no comment"),
    ],
)
def test_strip_comment(line: str, code: str) -> None:
    assert strip_comment(line) == code
