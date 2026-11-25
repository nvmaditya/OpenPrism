"""A guard that latexmk runs instead of `biber`, to close two holes in it.

Biber is a separate Perl program; none of the TeX hardening (openin_any,
shell_escape, the scrubbed env) reaches it. Two problems follow from that
(both verified on TeX Live 2023 / Biber 2.19, see docs/compile.md):

* Biber opens the data sources named in ``\\addbibresource`` itself, with no
  kpathsea check, so an absolute, ``..`` or remote path reads outside the
  project.
* Biber turns a data source's ``type``/``datatype`` attribute from the
  control file (``.bcf``) into a Perl package name and runs it through an
  ``eval``, so a crafted ``\\addbibresource[datatype=...]`` can execute code.

latexmk is told (in ``prismtex.compile.argv``) to run biber as
``<python> -P -m prismtex.compile.biberguard %O %S``. This module parses the
``.bcf`` first, refuses an unsafe data source, and only then replaces itself
with the real ``biber --noconf`` (``--noconf`` stops a project ``biber.conf``
from redirecting biber's input and output). It is run by `-P`, so the project
folder -- biber's working directory -- cannot shadow the ``prismtex`` package.

It is a command-line program (``main``), not part of the server process.
"""

from __future__ import annotations

import os
import shutil
import sys
import xml.etree.ElementTree as ET

__all__ = ["DataSourceRejected", "check_bcf", "main"]

# Data-source attribute values biblatex writes are short identifiers
# (``file``, ``bibtex``, ``false``, ...). Anything outside this set is how the
# eval-based code execution gets in, so it is refused.
_SAFE_ATTR = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_REMOTE_PREFIXES = ("http://", "https://", "ftp://", "ftps://", "file://")


class DataSourceRejected(Exception):
    """A ``.bcf`` names a data source the guard will not let biber open."""


def _is_remote(name: str) -> bool:
    return name.lower().startswith(_REMOTE_PREFIXES)


def _is_absolute(name: str) -> bool:
    # Covers POSIX ``/x``, Windows ``C:\\x`` and ``C:/x``, and UNC ``\\\\host``.
    return os.path.isabs(name) or (len(name) >= 2 and name[1] == ":") or name.startswith("\\\\")


def _has_parent_ref(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return ".." in parts


def _localname(tag: str) -> str:
    return tag.rpartition("}")[2]


def check_bcf(path: str) -> None:
    """Parse the ``.bcf`` at `path`; raise DataSourceRejected if it is unsafe.

    The parse itself failing is treated as unsafe (fail closed): biber would
    read the same file, so a control file this guard cannot vet is refused.
    """
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - no external entities are resolved
    except (OSError, ET.ParseError) as exc:
        raise DataSourceRejected(f"the control file could not be read: {exc}") from exc

    sources = [el for el in root.iter() if _localname(el.tag) == "datasource"]
    for element in sources:
        for attr, value in element.attrib.items():
            if any(ch not in _SAFE_ATTR for ch in value):
                raise DataSourceRejected(
                    f"data-source attribute {_localname(attr)!r} has an unexpected value"
                )
        name = (element.text or "").strip()
        if not name:
            continue
        if _is_remote(name):
            raise DataSourceRejected("a remote data source is not allowed")
        if _is_absolute(name):
            raise DataSourceRejected("an absolute data-source path is not allowed")
        if _has_parent_ref(name):
            raise DataSourceRejected("a data-source path may not contain '..'")


def _control_file(args: list[str]) -> str | None:
    """The ``.bcf`` among biber's arguments (latexmk passes it as the last one)."""
    candidates = [a for a in args if not a.startswith("-")]
    for arg in reversed(candidates):
        if arg.lower().endswith(".bcf"):
            return arg
    # latexmk normally passes the control file with its ``.bcf`` suffix; if it
    # passed a bare job name, the control file sits beside it.
    if candidates:
        guess = candidates[-1] + ".bcf"
        return guess if os.path.isfile(guess) else None
    return None


def main(argv: list[str] | None = None) -> int:
    """Vet the control file, then exec the real ``biber --noconf`` (never returns on success)."""
    args = list(sys.argv[1:] if argv is None else argv)
    control = _control_file(args)
    if control is None:
        sys.stderr.write("prismtex biber guard: no .bcf control file was given to biber.\n")
        return 2
    try:
        check_bcf(control)
    except DataSourceRejected as exc:
        sys.stderr.write(
            f"prismtex biber guard: refusing to run biber on {os.path.basename(control)}: "
            f"{exc}. A project may only cite .bib files inside its own folder.\n"
        )
        return 2

    biber = shutil.which("biber")
    if biber is None:
        sys.stderr.write("prismtex biber guard: biber was not found on PATH.\n")
        return 2
    os.execv(biber, [biber, "--noconf", *args])


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
