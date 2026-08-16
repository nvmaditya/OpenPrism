"""Agent tools: compile, static check, PDF text, add section + \\input."""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent.verify import check_latex, extract_project_pdf_text, pick_compile_engine
from app.compile.engine import CompileEngine
from app.domain.project import ProjectError, ProjectStore


class PathOptArgs(BaseModel):
    path: str | None = Field(
        default=None,
        description="Relative .tex path to check (default: all .tex files)",
    )


class PdfArgs(BaseModel):
    path: str = Field(
        default="main.pdf",
        description="Relative PDF path inside the project (usually main.pdf)",
    )


class CompileArgs(BaseModel):
    main_file: str = Field(default="main.tex", description="Main .tex file to compile")
    engine: str | None = Field(
        default=None,
        description="pdflatex or latexmk; default is whatever is installed",
    )


class AddInputArgs(BaseModel):
    path: str = Field(
        description="New relative .tex path (e.g. sections/projects.tex)"
    )
    content: str = Field(
        description="Section fragment (no \\documentclass). Will be \\input from main.tex"
    )
    main_file: str = Field(default="main.tex", description="Skeleton file to add \\input")


def make_tex_tools(
    store: ProjectStore,
    project_id: str,
    compile_engine: CompileEngine,
) -> list[StructuredTool]:
    def check_latex_tool(path: str | None = None) -> str:
        """Static-check one or all project .tex files."""
        try:
            targets = [path] if path else [
                p for p in store.list_files(project_id) if p.endswith(".tex")
            ]
            all_errs: list[str] = []
            for rel in targets:
                raw = store.read_file(project_id, rel)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    all_errs.append(f"{rel}: not UTF-8")
                    continue
                for err in check_latex(text, path=rel):
                    all_errs.append(f"{rel}: {err}")
            if all_errs:
                return "ERROR [latex]: " + "; ".join(all_errs)
            return "OK latex " + ", ".join(targets)
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def compile_project_tool(
        main_file: str = "main.tex", engine: str | None = None
    ) -> str:
        """Compile the project with an allowlisted TeX engine."""
        chosen = engine or pick_compile_engine()
        if not chosen:
            return "ERROR [skipped]: no pdflatex/latexmk on PATH"
        try:
            result = compile_engine.compile(
                project_id, engine=chosen, main_file=main_file  # type: ignore[arg-type]
            )
            excerpt = (result.log_text or "")[-1500:]
            if result.success:
                return (
                    f"OK compiled {main_file} pdf={result.pdf_path} "
                    f"log_id={result.log_id}\n{excerpt}"
                )
            return (
                f"ERROR [compile]: {main_file} failed log_id={result.log_id}\n{excerpt}"
            )
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"

    def extract_pdf_text_tool(path: str = "main.pdf") -> str:
        """Extract text from a compiled PDF in the project (not markdown)."""
        try:
            text = extract_project_pdf_text(store, project_id, path)
            if len(text) > 8000:
                return text[:8000] + "\n… [truncated]"
            return text or "(empty PDF text)"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR [pdf]: {exc}"

    def add_tex_input(
        path: str, content: str, main_file: str = "main.tex"
    ) -> str:
        """Create a section file and add one \\input in main.tex (preamble untouched)."""
        rel = path.replace("\\", "/")
        if ".." in rel.split("/"):
            return "ERROR [path_escape]: path must stay inside the project"
        body = content.replace("\r\n", "\n")
        if rel.startswith("sections/") and r"\documentclass" in body:
            return "ERROR [invalid]: section files must not contain \\documentclass"
        try:
            store.write_file(project_id, rel, body)
            main = store.read_file(project_id, main_file).decode("utf-8")
        except UnicodeDecodeError:
            return f"ERROR [invalid]: {main_file} is not UTF-8"
        except ProjectError as exc:
            return f"ERROR [{exc.code}]: {exc.message}"
        key = rel[:-4] if rel.endswith(".tex") else rel
        already = rf"\input{{{key}}}" in main or rf"\input{{{rel}}}" in main
        if not already:
            if r"\end{document}" not in main:
                return f"ERROR [invalid]: {main_file} missing \\end{{document}}"
            line = f"\\input{{{key}}}\n"
            main = main.replace(r"\end{document}", line + r"\end{document}", 1)
            store.write_file(project_id, main_file, main.replace("\r\n", "\n"))
        return f"OK wrote {rel} and input {key} in {main_file}"

    return [
        StructuredTool.from_function(
            func=check_latex_tool,
            name="check_latex",
            description=(
                "Static-check .tex files (braces, environments, \\end{document}). "
                "Call after edits. Does not compile."
            ),
            args_schema=PathOptArgs,
        ),
        StructuredTool.from_function(
            func=compile_project_tool,
            name="compile_project",
            description=(
                "Compile the project (pdflatex/latexmk). Use to verify the PDF builds. "
                "The host also compiles after the turn; you cannot skip that gate."
            ),
            args_schema=CompileArgs,
        ),
        StructuredTool.from_function(
            func=extract_pdf_text_tool,
            name="extract_pdf_text",
            description=(
                "Extract text from the compiled PDF to confirm phrases/headings landed. "
                "Requires a successful compile first."
            ),
            args_schema=PdfArgs,
        ),
        StructuredTool.from_function(
            func=add_tex_input,
            name="add_tex_input",
            description=(
                "Create a NEW section .tex file and add \\input{...} to main.tex "
                "just before \\end{document}. Never rewrites the preamble. "
                "Section files must not contain \\documentclass."
            ),
            args_schema=AddInputArgs,
        ),
    ]
