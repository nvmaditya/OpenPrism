"""The latexmk command line and the environment it runs in (pure functions).

These are the only places the compile argv and environment are built, and
both take validated inputs only. The flags and variables close the routes by
which a project could run code or touch files outside its folder; see
docs/compile.md for what each one does and how it was verified.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from prismtex.workspace import ENGINES, Engine

LATEXMK = "latexmk"

ENGINE_FLAGS: Mapping[Engine, tuple[str, ...]] = {
    "pdflatex": ("-pdf",),
    "xelatex": ("-pdfxe",),
    # One argv element: latexmk substitutes %O (options) and %S (source).
    "lualatex": ("-pdflua", "-lualatex=lualatex --safer --nosocket %O %S"),
}

# latexmk runs the engine through `sh -c` (cmd.exe on Windows) with the file
# name inside double quotes. These characters keep a special meaning there:
# a backtick in a root file name runs a command (verified 2026-11-25).
SHELL_UNSAFE_CHARS = frozenset('`$\\"%!')

_KEPT_VARS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TEXMFHOME",
        "TEXMFVAR",
        "TEXMFCONFIG",
    }
)
_KEPT_PREFIXES = ("MIKTEX_",)


def is_shell_safe_name(name: str) -> bool:
    """True when `name` can sit inside double quotes in a latexmk command."""
    return not any(ch in SHELL_UNSAFE_CHARS or ord(ch) < 32 for ch in name)


def build_latexmk_argv(root_file: str, engine: Engine, out_dir: Path) -> list[str]:
    """`latexmk -norc <engine flag> ... -outdir=<abs> <root_file>`.

    Raises ValueError for inputs that should never get this far: callers
    validate the root file with `check_root_file` first.
    """
    if engine not in ENGINES:
        raise ValueError(f"unknown engine: {engine!r}")
    if not root_file or root_file.startswith("-"):
        raise ValueError("the root file must be a non-empty name that does not start with '-'")
    if not is_shell_safe_name(root_file):
        raise ValueError("the root file name contains a character latexmk's shell would expand")
    return [
        LATEXMK,
        "-norc",
        *ENGINE_FLAGS[engine],
        "-interaction=nonstopmode",
        "-file-line-error",
        "-synctex=1",
        "-no-shell-escape",
        f"-outdir={os.path.abspath(out_dir)}",
        root_file,
    ]


def build_tex_env(base: Mapping[str, str], out_dir: Path) -> dict[str, str]:
    """The environment for latexmk: an allowlist of `base` plus the TeX hardening.

    Everything not on the list is dropped, provider keys included, because
    LuaTeX's `os.getenv` can read the environment.
    """
    env = {
        name: value
        for name, value in base.items()
        if name in _KEPT_VARS or name.startswith(_KEPT_PREFIXES)
    }
    env.update(
        {
            "openin_any": "p",
            "openout_any": "p",
            "shell_escape": "f",
            "TEXMFOUTPUT": os.path.abspath(out_dir),
            "max_print_line": "10000",
            "error_line": "254",
            "half_error_line": "238",
        }
    )
    return env
