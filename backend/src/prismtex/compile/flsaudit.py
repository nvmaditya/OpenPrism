"""Post-run audit that TeX read only files inside the allowed roots.

Nothing in the argv and env hardening stops a read outside the project.
pdfTeX's ``\\pdfobj ... file`` never went through kpathsea's ``openin_any``
check, and since TeX Live 2026 kpathsea ignores ``openin_any`` altogether, so
``\\input`` and ``\\openin`` read any file the server account can read (see
docs/compile.md). latexmk runs the engine with ``-recorder``, which lists
every file the engine opened as an ``INPUT`` line in ``<stem>.fls``. After the
run this module resolves each INPUT and checks it against the places a
compile may legitimately read: the project folder, the build (out) folder,
the TeX installation's own trees (``kpsewhich -var-value=TEXMF``) and
kpathsea's texmf.cnf files. A read anywhere else means the PDF or the log may
carry a file it should not, so the caller discards both and fails the compile.

This is a mitigation, not a sandbox. It audits after the run, so a read that
happens on an earlier latexmk pass but not the final one can be missed, and it
cannot see a read made without the recorder (LuaTeX's Lua layer, which is why
lualatex is refused outright). The real fix is an OS-level sandbox, tracked as
a follow-up. See docs/compile.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

__all__ = ["AUDITED_ENGINES", "audit_inputs"]

# Only the engines whose reads the recorder captures are audited. lualatex is
# refused before it runs (its Lua I/O is not recorded), so it never reaches
# here; it is left out so a future caller cannot audit it by mistake.
AUDITED_ENGINES = frozenset({"pdflatex", "xelatex"})

# Most offending paths an attacker can name are few; this bounds the work and
# the diagnostic if a document reads many.
_MAX_REPORTED = 20

# TeX trees are discovered at runtime from kpsewhich; these are a floor so the
# audit still has the standard Debian/TeX Live trees if kpsewhich cannot run.
_STATIC_ROOTS = (
    "/etc/texmf",
    "/var/lib/texmf",
    "/usr/share/texmf",
    "/usr/local/share/texmf",
    "/usr/share/texlive",
)
_TEXMF_VARS = (
    "TEXMF",
    "TEXMFDIST",
    "TEXMFMAIN",
    "TEXMFLOCAL",
    "TEXMFSYSVAR",
    "TEXMFSYSCONFIG",
    "TEXMFVAR",
    "TEXMFHOME",
    "TEXMFCONFIG",
)

# Variables that change with every compile and say nothing about where the TeX
# trees are; they are left out of the kpsewhich query and its cache key.
_PER_RUN_VARS = frozenset({"TEXMFOUTPUT"})

_roots_cache: dict[tuple[str, tuple[tuple[str, str], ...]], frozenset[str]] = {}
_roots_lock = threading.Lock()


def _expand(value: str) -> list[str]:
    """Absolute trees in a ``kpsewhich -var-value`` result (TEXMF is brace-listed)."""
    cleaned = value.replace("{", " ").replace("}", " ")
    trees: list[str] = []
    for part in cleaned.replace(",", " ").split():
        name = part.lstrip("!").strip()
        if name and os.path.isabs(name):
            trees.append(name)
    return trees


def _kpsewhich(kpsewhich: str, args: list[str], env: Mapping[str, str]) -> str:
    try:
        return subprocess.run(
            [kpsewhich, *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(env),
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _cached(
    kind: str,
    kpsewhich: str | None,
    env: Mapping[str, str],
    compute: Callable[[str, dict[str, str]], frozenset[str]],
) -> frozenset[str]:
    """`compute(kpsewhich, query_env)`, cached per kind, executable and environment.

    kpsewhich runs in the compile's own environment: the per-user trees
    (TEXMFHOME, TEXMFVAR, TEXMFCONFIG) expand from HOME or USERPROFILE, and
    TeX reads fonts it generated at run time from TEXMFVAR. Querying with a
    different environment would put those trees somewhere TeX never looked.
    """
    query_env = {name: value for name, value in env.items() if name not in _PER_RUN_VARS}
    key = (f"{kind}:{kpsewhich or ''}", tuple(sorted(query_env.items())))
    with _roots_lock:
        cached = _roots_cache.get(key)
    if cached is not None:
        return cached
    result = compute(kpsewhich, query_env) if kpsewhich is not None else frozenset()
    with _roots_lock:
        _roots_cache[key] = result
    return result


def _texmf_roots(kpsewhich: str | None, env: Mapping[str, str]) -> frozenset[str]:
    """Real paths of the TeX trees as the compile saw them."""

    def compute(exe: str, query_env: dict[str, str]) -> frozenset[str]:
        found: set[str] = set()
        for var in _TEXMF_VARS:
            found.update(_expand(_kpsewhich(exe, [f"-var-value={var}"], query_env)))
        return frozenset(found)

    found = set(_cached("trees", kpsewhich, env, compute)) | set(_STATIC_ROOTS)
    return frozenset(os.path.realpath(path) for path in found if os.path.isdir(path))


def _config_files(kpsewhich: str | None, env: Mapping[str, str]) -> frozenset[str]:
    """Real paths of the texmf.cnf files kpathsea reads.

    TeX Live keeps its main texmf.cnf at the top of the installation, above
    every tree, and the recorder lists it. Only these exact files are allowed,
    not the folders around them.
    """

    def compute(exe: str, query_env: dict[str, str]) -> frozenset[str]:
        listed = _kpsewhich(exe, ["-all", "texmf.cnf"], query_env).splitlines()
        return frozenset(
            os.path.realpath(line.strip())
            for line in listed
            if line.strip() and os.path.isabs(line.strip()) and os.path.isfile(line.strip())
        )

    return _cached("config", kpsewhich, env, compute)


def _input_paths(fls_text: str) -> tuple[str | None, list[str]]:
    """The recorder's working directory and the raw INPUT paths from one ``.fls``."""
    pwd: str | None = None
    inputs: list[str] = []
    for line in fls_text.splitlines():
        if line.startswith("PWD "):
            pwd = line[4:].strip() or pwd
        elif line.startswith("INPUT "):
            inputs.append(line[6:].strip())
    return pwd, inputs


def _inside(resolved: str, roots: frozenset[str]) -> bool:
    candidate = Path(resolved)
    return any(candidate == Path(root) or candidate.is_relative_to(root) for root in roots)


def audit_inputs(out_dir: Path, src_dir: Path, *, env: Mapping[str, str]) -> list[str]:
    """Raw INPUT paths from the ``.fls`` files that resolve outside the allowed roots.

    An empty list means every recorded read was inside the project, the build
    folder or a TeX tree. The returned strings are the paths as the recorder
    wrote them (the document's own text), not the resolved server paths, so a
    diagnostic built from them leaks no absolute server location.
    """
    path_env = env.get("PATH")
    kpsewhich = shutil.which("kpsewhich", path=path_env) if path_env else None
    allowed = _texmf_roots(kpsewhich, env) | {
        os.path.realpath(src_dir),
        os.path.realpath(out_dir),
    }
    allowed_files = _config_files(kpsewhich, env)
    offending: list[str] = []
    seen: set[str] = set()
    for fls in sorted(out_dir.glob("*.fls")):
        try:
            text = fls.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pwd, inputs = _input_paths(text)
        base = pwd if (pwd and os.path.isabs(pwd)) else os.fspath(src_dir)
        for raw in inputs:
            resolved = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
            if resolved in allowed_files or _inside(resolved, allowed):
                continue
            if raw not in seen:
                seen.add(raw)
                offending.append(raw)
    offending.sort()
    return offending[:_MAX_REPORTED]
