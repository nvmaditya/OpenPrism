"""The latexmk command line and the environment it runs in (pure functions).

These are the only places the compile argv and environment are built, and
both take validated inputs only. The flags and variables close the routes by
which a project could run code or touch files outside its folder; see
docs/compile.md for what each one does and how it was verified.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import prismtex
from prismtex.workspace import ENGINES, Engine

LATEXMK = "latexmk"

# The directory that holds the `prismtex` package, put on PYTHONPATH so the
# biber guard (`python -P -m prismtex.compile.biberguard`) resolves even where
# prismtex is not installed. `-P` keeps the project folder off sys.path, so it
# cannot shadow the package.
_PRISMTEX_PARENT = str(Path(prismtex.__file__).resolve().parent.parent)

# latexmk's `-pdf`/`-pdfxe`/`-pdflua`, and the option that names the engine
# command. Passing the engine (and the helper tools below) by absolute path
# stops Windows from running a same-named file from the project folder, which
# is latexmk's working directory (CreateProcess and cmd.exe search the current
# directory before PATH).
_PDF_FLAG: Mapping[Engine, str] = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}
_ENGINE_OPT: Mapping[Engine, str] = {
    "pdflatex": "-pdflatex=",
    "xelatex": "-xelatex=",
    "lualatex": "-lualatex=",
}
_ENGINE_SUFFIX: Mapping[Engine, str] = {
    "pdflatex": "%O %S",
    "xelatex": "%O %S",
    "lualatex": "--safer --nosocket %O %S",
}
_ENGINE_TOOL: Mapping[Engine, str] = {
    "pdflatex": "pdflatex",
    "xelatex": "xelatex",
    "lualatex": "lualatex",
}
# The programs latexmk starts besides the engine. biber is special: it is
# routed through the guard (see _biber_assignment), not run directly.
HELPER_TOOLS = ("bibtex", "makeindex", "kpsewhich")
RESOLVED_TOOLS = ("pdflatex", "xelatex", "lualatex", "biber", *HELPER_TOOLS)

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


def resolve_tools(path_env: str | None) -> dict[str, str]:
    """Absolute paths of the TeX programs found on `path_env`, keyed by name.

    A program not found, or whose path could not sit safely in a latexmk
    command, is left out: the argv then falls back to its bare name.
    """
    resolved: dict[str, str] = {}
    for name in RESOLVED_TOOLS:
        found = shutil.which(name, path=path_env) if path_env else shutil.which(name)
        if found is None:
            continue
        normalized = found.replace("\\", "/")  # latexmk and cmd.exe accept forward slashes
        if is_shell_safe_name(normalized) and " " not in normalized:
            resolved[name] = normalized
    return resolved


# What latexmk runs for biber when the guard cannot be named safely in its
# command (a Python path with a space or a shell character, as under
# `C:\Program Files` on Windows). latexmk's NONE keyword runs nothing and fails
# the biber rule, so biber is unavailable rather than run unguarded.
BIBER_UNAVAILABLE = (
    "NONE biber is unavailable because the PrismTeX biber guard cannot be run "
    "from a Python path that contains a space or a shell character"
)


def _biber_assignment() -> list[str]:
    """The `-e` assignment that routes biber through the guard, or disables it.

    It is always emitted: leaving `$biber` unset would let latexmk run the real
    biber, which executes code from a crafted .bcf (see biberguard.py).
    """
    python = sys.executable.replace("\\", "/")
    if python and is_shell_safe_name(python) and " " not in python:
        return ["-e", f"$biber=q{{{python} -P -m prismtex.compile.biberguard %O %S}}"]
    return ["-e", f"$biber=q{{{BIBER_UNAVAILABLE}}}"]


def _tool_assignments(tools: Mapping[str, str]) -> list[str]:
    """`-e` assignments that pin biber (to the guard) and the helper tools by path.

    The `%O`/`%S`/`%D` placeholders are latexmk's own and are meant literally;
    only the program paths are checked (they must sit safely in the command).
    """
    out: list[str] = [*_biber_assignment()]
    for name, template in (("bibtex", "{cmd} %O %S"), ("makeindex", "{cmd} %O -o %D %S")):
        cmd = tools.get(name)
        if cmd is not None:
            out += ["-e", f"${name}=q{{{template.format(cmd=cmd)}}}"]
    kpsewhich = tools.get("kpsewhich")
    if kpsewhich is not None:
        out += ["-e", f"$kpsewhich=q{{{kpsewhich} %S}}"]
    return out


def build_latexmk_argv(
    root_file: str,
    engine: Engine,
    out_dir: Path,
    *,
    tools: Mapping[str, str] | None = None,
) -> list[str]:
    """`latexmk -norc <engine flag> ... -outdir=<abs> <root_file>`.

    `tools` maps a TeX program name to its absolute path (from `resolve_tools`);
    where it is missing a program, that program's bare name is used, as before.
    biber is always routed through the guard. Raises ValueError for inputs that
    should never get this far: callers validate the root file with
    `check_root_file` first.
    """
    if engine not in ENGINES:
        raise ValueError(f"unknown engine: {engine!r}")
    if not root_file or root_file.startswith("-"):
        raise ValueError("the root file must be a non-empty name that does not start with '-'")
    if not is_shell_safe_name(root_file):
        raise ValueError("the root file name contains a character latexmk's shell would expand")
    tools = tools or {}
    engine_cmd = tools.get(_ENGINE_TOOL[engine], _ENGINE_TOOL[engine])
    return [
        LATEXMK,
        "-norc",
        _PDF_FLAG[engine],
        f"{_ENGINE_OPT[engine]}{engine_cmd} {_ENGINE_SUFFIX[engine]}",
        *_tool_assignments(tools),
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
            # kpathsea writes missfont.log into the current directory (the
            # project folder) when it cannot build a font; 0 turns that off so
            # the project folder stays free of build artifacts (ADR 0004).
            "MISSFONT_LOG": "0",
            # Windows: keep the current directory out of the executable search
            # path, so a project file cannot be run as a tool.
            "NoDefaultCurrentDirectoryInExePath": "1",
            # So `python -P -m prismtex.compile.biberguard` finds the package.
            "PYTHONPATH": _PRISMTEX_PARENT,
        }
    )
    return env
