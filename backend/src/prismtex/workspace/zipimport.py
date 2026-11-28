"""ZIP import: every check runs before anything is unpacked.

An imported archive is untrusted. `ValidatedZip.open` rejects zip-slip
names and symlinks, strips a shared top folder, leaves out the
`conversations/` folder of an OpenAI Prism export, then rejects encrypted
entries, archives over the size or file-count limits and entries that look
like ZIP bombs. Only then may the caller create a project and `extract_to` it.
"""

from __future__ import annotations

import re
import shutil
import stat
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Self, get_args

from prismtex.errors import ImportRejected, PathPolicyError
from prismtex.workspace.paths import check_relative_path, is_internal
from prismtex.workspace.text import strip_comment

__all__ = ["IMPORT_SOURCES", "ImportSource", "ValidatedZip", "detect_root_file"]

# Where an archive comes from. "auto" means "prism" when the archive has a
# `conversations/` folder at the top after the top-folder strip, else "overleaf".
ImportSource = Literal["auto", "overleaf", "prism"]
IMPORT_SOURCES: frozenset[str] = frozenset(get_args(ImportSource))
# An OpenAI Prism export keeps its chat history here; it is not project content.
_CONVERSATIONS = "conversations/"

MAX_RATIO = 100
RATIO_MIN_BYTES = 2**20
_CHUNK = 2**16
_DOCUMENTCLASS = re.compile(r"\\documentclass(?![A-Za-z@])")
# Unpacking can fail half-way on a damaged archive even after the checks pass.
_EXTRACT_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)

# End-of-central-directory signatures and sizes, for counting entries from the
# archive tail before `zipfile` materializes a ZipInfo for every one of them.
_EOCD_SIG = b"PK\x05\x06"
_EOCD_SIZE = 22
_MAX_COMMENT = 0xFFFF
_Z64_LOCATOR_SIG = b"PK\x06\x07"
_Z64_LOCATOR_SIZE = 20
_Z64_EOCD_SIG = b"PK\x06\x06"
_CD_RECORD_MIN = 46  # the fixed part of one central-directory file header


@dataclass(frozen=True)
class _Entry:
    info: zipfile.ZipInfo
    path: str


class ValidatedZip:
    """An archive that passed every import check; use as a context manager."""

    def __init__(self, archive: zipfile.ZipFile, entries: list[_Entry]) -> None:
        self._archive = archive
        self._entries = entries

    @classmethod
    def open(
        cls,
        stream: BinaryIO,
        *,
        max_bytes: int,
        max_files: int,
        source: ImportSource = "auto",
    ) -> Self:
        # Count entries from the archive tail first. `zipfile.ZipFile` reads the
        # whole central directory into memory, so a hostile archive that claims
        # millions of entries would exhaust RAM before any limit applied.
        _check_entry_count(stream, max_files=max_files)
        try:
            archive = zipfile.ZipFile(stream)
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise ImportRejected("bad_zip", "The file is not a valid ZIP archive.") from exc
        try:
            entries = _plan(
                archive.infolist(), max_bytes=max_bytes, max_files=max_files, source=source
            )
        except BaseException:
            archive.close()
            raise
        return cls(archive, entries)

    @property
    def paths(self) -> list[str]:
        """Destination paths of the files to unpack, top folder stripped."""
        return [entry.path for entry in self._entries]

    def extract_to(self, dest: Path) -> None:
        """Unpack into `dest`, a fresh folder that holds no symlinks."""
        for entry in self._entries:
            target = dest.joinpath(*entry.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self._archive.open(entry.info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
            except _EXTRACT_ERRORS as exc:
                raise ImportRejected(
                    "bad_zip", f"The archive is damaged and '{entry.info.filename}' cannot be read."
                ) from exc
            except OSError as exc:
                # A name that is too long for the file system, or a file/folder
                # case clash on a case-insensitive disk, surfaces here. Report it
                # as a rejected import rather than letting it become a 500.
                raise ImportRejected(
                    "bad_zip",
                    f"'{entry.info.filename}' cannot be written on this file system.",
                ) from exc

    def close(self) -> None:
        self._archive.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _check_entry_count(stream: BinaryIO, *, max_files: int) -> None:
    """Reject an archive whose central directory names far too many entries.

    Read only the end-of-central-directory record (and the ZIP64 record it may
    point to) to learn the total entry count without parsing the directory. The
    cap includes directories and skipped names, so it is generous: `2 * max_files`.
    """
    total = _central_directory_count(stream)
    if total is not None and total > 2 * max_files:
        raise ImportRejected("too_many_files", f"The archive has more than {max_files} files.")


def _central_directory_count(stream: BinaryIO) -> int | None:
    """The total central-directory entry count from the archive tail, or None.

    None means the record could not be read here; `zipfile` then reports the
    archive as invalid in the usual way.
    """
    stream.seek(0, 2)
    size = stream.tell()
    window = min(size, _EOCD_SIZE + _MAX_COMMENT)
    stream.seek(size - window)
    tail = stream.read(window)
    stream.seek(0)
    idx = tail.rfind(_EOCD_SIG)
    if idx < 0 or window - idx < _EOCD_SIZE:
        return None
    eocd = tail[idx : idx + _EOCD_SIZE]
    total, cd_size = struct.unpack("<HI", eocd[10:16])
    if total != 0xFFFF and cd_size != 0xFFFFFFFF:
        # A central directory cannot be smaller than one fixed header per entry;
        # a smaller one is malformed (and `zipfile` will reject it as such).
        if cd_size < total * _CD_RECORD_MIN:
            return None
        return total
    return _zip64_count(stream, tail, idx, fallback=total)


def _zip64_count(stream: BinaryIO, tail: bytes, eocd_idx: int, *, fallback: int) -> int | None:
    """The 8-byte entry count from the ZIP64 end-of-central-directory record."""
    loc = eocd_idx - _Z64_LOCATOR_SIZE
    if loc < 0 or tail[loc : loc + 4] != _Z64_LOCATOR_SIG:
        return fallback
    z64_offset = struct.unpack("<Q", tail[loc + 8 : loc + 16])[0]
    stream.seek(0, 2)
    if z64_offset + 40 > stream.tell():
        return None
    stream.seek(z64_offset)
    header = stream.read(56)
    stream.seek(0)
    if len(header) < 40 or header[:4] != _Z64_EOCD_SIG:
        return fallback
    return struct.unpack("<Q", header[32:40])[0]


def _plan(
    infos: list[zipfile.ZipInfo], *, max_bytes: int, max_files: int, source: ImportSource
) -> list[_Entry]:
    kept = _strip_top_folder(_members(infos, max_files=max_files))
    # Dropped before the limits are checked, so the limits count only kept entries.
    if _resolve_source(source, kept) == "prism":
        kept = [entry for entry in kept if not entry.path.startswith(_CONVERSATIONS)]
    _check_limits(kept, max_bytes=max_bytes, max_files=max_files)
    # Old OpenPrism exports carried app metadata in `.prism/`; it is not user content.
    kept = [entry for entry in kept if not is_internal(entry.path)]
    if not kept:
        raise ImportRejected("empty", "The archive contains no files.")
    return _dedupe(kept)


def _members(infos: list[zipfile.ZipInfo], *, max_files: int) -> list[_Entry]:
    """The file entries with their checked paths; links and unsafe names are rejected."""
    members: list[_Entry] = []
    seen = 0
    for info in infos:
        name = info.filename
        # Every record, directories and skipped names included, counts toward
        # the work cap; otherwise an archive of nothing but directories slips past.
        seen += 1
        if seen > 2 * max_files:
            raise ImportRejected("too_many_files", f"The archive has more than {max_files} files.")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ImportRejected("symlink", f"The archive contains a symbolic link: '{name}'.")
        try:
            path = check_relative_path(name.replace("\\", "/").rstrip("/"))
        except PathPolicyError as exc:
            raise ImportRejected(
                "zip_slip", f"The archive contains a path that is not allowed: '{name}'."
            ) from exc
        if _skipped(info, path):
            continue
        members.append(_Entry(info, path))
    return members


def _resolve_source(source: ImportSource, entries: list[_Entry]) -> Literal["overleaf", "prism"]:
    if source != "auto":
        return source
    has_conversations = any(entry.path.startswith(_CONVERSATIONS) for entry in entries)
    return "prism" if has_conversations else "overleaf"


def _check_limits(entries: list[_Entry], *, max_bytes: int, max_files: int) -> None:
    total = 0
    for count, entry in enumerate(entries, start=1):
        info = entry.info
        name = info.filename
        if info.flag_bits & 0x1:
            raise ImportRejected("bad_zip", "Encrypted archives cannot be imported.")
        if (
            info.file_size > RATIO_MIN_BYTES
            and info.file_size / max(info.compress_size, 1) > MAX_RATIO
        ):
            raise ImportRejected(
                "ratio", f"'{name}' is compressed suspiciously well; it may be a ZIP bomb."
            )
        total += info.file_size
        if total > max_bytes:
            raise ImportRejected(
                "too_large", f"The archive unpacks to more than {_mb(max_bytes)} MB."
            )
        if count > max_files:
            raise ImportRejected("too_many_files", f"The archive has more than {max_files} files.")


def _skipped(info: zipfile.ZipInfo, path: str) -> bool:
    segments = path.split("/")
    return (
        info.is_dir()
        or info.filename.endswith(("/", "\\"))
        or segments[0] == "__MACOSX"
        or segments[-1] == ".DS_Store"
        or any(seg.lower() == ".git" for seg in segments)
    )


def _strip_top_folder(entries: list[_Entry]) -> list[_Entry]:
    firsts = {entry.path.split("/", 1)[0] for entry in entries}
    if len(firsts) != 1 or any("/" not in entry.path for entry in entries):
        return entries
    return [_Entry(entry.info, entry.path.split("/", 1)[1]) for entry in entries]


def _dedupe(entries: list[_Entry]) -> list[_Entry]:
    by_path: dict[str, _Entry] = {}
    for entry in entries:
        by_path[entry.path] = entry  # a later entry replaces an earlier one, as in unzip
    folders = {
        "/".join(path.split("/")[:i]) for path in by_path for i in range(1, path.count("/") + 1)
    }
    clash = sorted(folders & by_path.keys())
    if clash:
        raise ImportRejected(
            "bad_zip", f"The archive has both a file and a folder named '{clash[0]}'."
        )
    # On a case-insensitive file system (NTFS, APFS) a file and a folder whose
    # names differ only in case collide when unpacked, which would otherwise
    # fail mid-extraction with a 500. Reject it up front on every OS.
    folder_by_lower = {folder.casefold(): folder for folder in folders}
    for file_path in by_path:
        folder = folder_by_lower.get(file_path.casefold())
        if folder is not None and folder != file_path:
            first, second = sorted((file_path, folder))
            raise ImportRejected(
                "bad_zip",
                "The archive has a file and a folder whose names differ only in case: "
                f"'{first}' and '{second}'.",
            )
    return list(by_path.values())


def _mb(n_bytes: int) -> str:
    mb = n_bytes / 2**20
    return str(int(mb)) if mb.is_integer() else f"{mb:.1f}"


def detect_root_file(paths: Iterable[str], read_text: Callable[[str], str]) -> str | None:
    """The `.tex` file with an uncommented `\\documentclass` that most likely is the root.

    Prefers `main.tex`, then the fewest path segments, then alphabetical order.
    """
    candidates = [
        path
        for path in paths
        if path.lower().endswith(".tex") and _has_documentclass(read_text(path))
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda p: (p.rsplit("/", 1)[-1].lower() != "main.tex", p.count("/"), p.lower(), p),
    )


def _has_documentclass(text: str) -> bool:
    return any(_DOCUMENTCLASS.search(strip_comment(line)) for line in text.splitlines())
