"""Post-run audit that TeX read only files inside the allowed roots.

pdfTeX's ``\\pdfobj ... file`` opens a file without kpathsea's ``openin_any``
check and can copy its bytes into the PDF, so the argv and env hardening does
not stop a read outside the project (verified on TeX Live 2023; see
docs/compile.md). latexmk runs the engine with ``-recorder``, which lists
every file the engine opened as an ``INPUT`` line in ``<stem>.fls``. After the
run this module resolves each INPUT and checks it against three places a
compile may legitimately read: the project folder, the build (out) folder,
and the TeX installation's own trees (``kpsewhich -var-value=TEXMF``). A read
anywhere else means the PDF may carry a file it should not, so the caller
discards the PDF and fails the compile.

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

_roots_cache: dict[str, frozenset[str]] = {}
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


def _texmf_roots(kpsewhich: str | None) -> frozenset[str]:
    """Real paths of the TeX trees, cached per kpsewhich executable."""
    key = kpsewhich or ""
    with _roots_lock:
        cached = _roots_cache.get(key)
    if cached is not None:
        return cached

    found: set[str] = set()
    if kpsewhich is not None:
        env = {"PATH": os.path.dirname(kpsewhich) or os.environ.get("PATH", "")}
        for var in _TEXMF_VARS:
            try:
                out = subprocess.run(
                    [kpsewhich, f"-var-value={var}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                    check=False,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            found.update(_expand(out))
    found.update(_STATIC_ROOTS)
    roots = frozenset(os.path.realpath(path) for path in found if os.path.isdir(path))
    with _roots_lock:
        _roots_cache[key] = roots
    return roots


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


def audit_inputs(out_dir: Path, src_dir: Path, *, path_env: str | None) -> list[str]:
    """Raw INPUT paths from the ``.fls`` files that resolve outside the allowed roots.

    An empty list means every recorded read was inside the project, the build
    folder or a TeX tree. The returned strings are the paths as the recorder
    wrote them (the document's own text), not the resolved server paths, so a
    diagnostic built from them leaks no absolute server location.
    """
    kpsewhich = shutil.which("kpsewhich", path=path_env) if path_env else shutil.which("kpsewhich")
    allowed = _texmf_roots(kpsewhich) | {
        os.path.realpath(src_dir),
        os.path.realpath(out_dir),
    }
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
            if not _inside(resolved, allowed) and raw not in seen:
                seen.add(raw)
                offending.append(raw)
    offending.sort()
    return offending[:_MAX_REPORTED]
