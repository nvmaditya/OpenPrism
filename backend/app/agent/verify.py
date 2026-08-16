"""Host-side LaTeX verify: snapshot/restore, static checks, structure, PDF text."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from app.compile.engine import CompileEngine, EngineName
from app.domain.project import ProjectError, ProjectStore

_SECTION_RE = re.compile(
    r"\\(?:section|subsection|subsubsection)\*?\{([^{}]+)\}"
)
_INPUT_RE = re.compile(r"\\(?:input|include)\{([^{}]+)\}")
_DOCCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{[^{}]+\}")
_USEPKG_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^{}]+)\}")
_ENV_RE = re.compile(r"\\(begin|end)\{([^{}]+)\}")
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}")
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:|/)")


def _strip_comments(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        i = 0
        cut = len(line)
        while i < len(line):
            if line[i] == "%" and (i == 0 or line[i - 1] != "\\"):
                # keep newline
                nl = "\n" if line.endswith("\n") else ""
                out.append(line[:i] + nl)
                break
            i += 1
        else:
            out.append(line)
    return "".join(out)


def _braces_balanced(text: str) -> bool:
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in "{}":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def check_latex(text: str, *, path: str = "") -> list[str]:
    """Return static error strings (empty means OK)."""
    errors: list[str] = []
    stripped = _strip_comments(text)
    if not _braces_balanced(stripped):
        errors.append("unbalanced braces")
    stack: list[str] = []
    for kind, name in _ENV_RE.findall(stripped):
        if kind == "begin":
            stack.append(name)
        elif not stack:
            errors.append(f"\\end{{{name}}} without \\begin")
        else:
            opened = stack.pop()
            if opened != name:
                errors.append(f"\\begin{{{opened}}} closed by \\end{{{name}}}")
    if stack:
        errors.append("unclosed environments: " + ", ".join(stack))
    if r"\begin{document}" in stripped and r"\end{document}" not in stripped:
        errors.append("missing \\end{document}")
    for inc in _INCLUDEGRAPHICS_RE.findall(stripped):
        if _ABS_PATH.match(inc.replace("\\", "/")):
            errors.append(f"absolute includegraphics path: {inc}")
    rel = path.replace("\\", "/")
    if rel.startswith("sections/") and r"\documentclass" in stripped:
        errors.append("section file must not contain \\documentclass")
    return errors


def tex_structure(text: str) -> dict[str, Any]:
    preamble = (
        text.split(r"\begin{document}", 1)[0] if r"\begin{document}" in text else ""
    )
    doc = _DOCCLASS_RE.search(text)
    return {
        "documentclass": doc.group(0) if doc else "",
        "packages": _USEPKG_RE.findall(text),
        "headings": _SECTION_RE.findall(text),
        "inputs": _INPUT_RE.findall(text),
        "preamble": preamble,
    }


def project_structure(tex_files: dict[str, str]) -> dict[str, Any]:
    headings: list[str] = []
    inputs: list[str] = []
    packages: list[str] = []
    documentclass = ""
    for path, text in tex_files.items():
        st = tex_structure(text)
        headings.extend(st["headings"])
        inputs.extend(st["inputs"])
        if path.replace("\\", "/").endswith("main.tex") or r"\begin{document}" in text:
            packages.extend(st["packages"])
            if st["documentclass"]:
                documentclass = st["documentclass"]
    return {
        "documentclass": documentclass,
        "packages": packages,
        "headings": headings,
        "inputs": inputs,
    }


def load_tex_map(store: ProjectStore, project_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in store.list_files(project_id):
        if not rel.endswith(".tex"):
            continue
        try:
            out[rel] = store.read_file(project_id, rel).decode("utf-8")
        except (UnicodeDecodeError, ProjectError):
            continue
    return out


def compare_structure(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if before.get("documentclass") and before["documentclass"] != after.get(
        "documentclass"
    ):
        errors.append("\\documentclass changed")
    missing_h = [h for h in before.get("headings", []) if h not in after.get("headings", [])]
    if missing_h:
        errors.append("removed headings: " + ", ".join(missing_h))
    missing_i = [i for i in before.get("inputs", []) if i not in after.get("inputs", [])]
    if missing_i:
        errors.append("removed \\input/\\include: " + ", ".join(missing_i))
    missing_p = [
        p for p in before.get("packages", []) if p not in after.get("packages", [])
    ]
    if missing_p:
        errors.append("removed packages: " + ", ".join(missing_p))
    return errors


def snapshot_text_files(store: ProjectStore, project_id: str) -> dict[str, bytes]:
    snap: dict[str, bytes] = {}
    for rel in store.list_files(project_id):
        try:
            snap[rel] = store.read_file(project_id, rel)
        except ProjectError:
            continue
    return snap


def restore_text_files(
    store: ProjectStore, project_id: str, snap: dict[str, bytes]
) -> None:
    current = set(store.list_files(project_id))
    for rel, data in snap.items():
        store.write_file(project_id, rel, data)
    for rel in current:
        if rel not in snap:
            try:
                store.delete_file(project_id, rel)
            except ProjectError:
                continue


def extract_pdf_text_from_bytes(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_project_pdf_text(
    store: ProjectStore, project_id: str, path: str = "main.pdf"
) -> str:
    data = store.read_file(project_id, path)
    return extract_pdf_text_from_bytes(data)


def pick_compile_engine() -> EngineName | None:
    if shutil.which("pdflatex"):
        return "pdflatex"
    if shutil.which("latexmk"):
        return "latexmk"
    return None


@dataclass
class VerifyResult:
    ok: bool
    static_ok: bool
    static_errors: list[str] = field(default_factory=list)
    structure_ok: bool = True
    structure_errors: list[str] = field(default_factory=list)
    compile_status: str = "skipped"  # ok | failed | skipped
    compile_log_id: str | None = None
    compile_excerpt: str = ""
    pdf_ok: bool = True
    pdf_errors: list[str] = field(default_factory=list)
    pdf_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "static_ok": self.static_ok,
            "static_errors": self.static_errors,
            "structure_ok": self.structure_ok,
            "structure_errors": self.structure_errors,
            "compile": self.compile_status,
            "compile_log_id": self.compile_log_id,
            "pdf_ok": self.pdf_ok,
            "pdf_errors": self.pdf_errors,
        }


def verify_project(
    store: ProjectStore,
    project_id: str,
    compile_engine: CompileEngine,
    *,
    before_structure: dict[str, Any] | None = None,
    main_file: str = "main.tex",
    required_phrases: list[str] | None = None,
    timeout_s: float = 90.0,
) -> VerifyResult:
    tex_map = load_tex_map(store, project_id)
    static_errors: list[str] = []
    for path, text in tex_map.items():
        for err in check_latex(text, path=path):
            static_errors.append(f"{path}: {err}")
    static_ok = not static_errors

    after = project_structure(tex_map)
    structure_errors = (
        compare_structure(before_structure, after) if before_structure else []
    )
    structure_ok = not structure_errors

    result = VerifyResult(
        ok=False,
        static_ok=static_ok,
        static_errors=static_errors,
        structure_ok=structure_ok,
        structure_errors=structure_errors,
    )
    if not static_ok or not structure_ok:
        result.ok = False
        return result

    engine = pick_compile_engine()
    if engine is None:
        result.compile_status = "skipped"
        result.ok = True
        return result

    try:
        compiled = compile_engine.compile(
            project_id,
            engine=engine,
            main_file=main_file,
            timeout_s=timeout_s,
        )
    except ProjectError as exc:
        result.compile_status = "failed"
        result.compile_excerpt = str(exc)
        result.ok = False
        return result

    result.compile_log_id = compiled.log_id
    result.compile_excerpt = (compiled.log_text or "")[-4000:]
    if not compiled.success:
        result.compile_status = "failed"
        result.ok = False
        return result

    result.compile_status = "ok"
    if compiled.pdf_path:
        try:
            result.pdf_text = extract_project_pdf_text(
                store, project_id, compiled.pdf_path
            )
        except (ProjectError, Exception) as exc:  # noqa: BLE001
            result.pdf_errors.append(f"pdf extract failed: {exc}")
            result.pdf_ok = False
            result.ok = False
            return result
    for phrase in required_phrases or []:
        if phrase and phrase not in result.pdf_text:
            result.pdf_errors.append(f"PDF missing phrase: {phrase}")
            result.pdf_ok = False
    result.ok = result.pdf_ok
    return result
