"""Render PDF pages into PRISM_PREVIEW_ROOT only (never project paper tree)."""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from app.domain.project import ProjectError, ProjectStore
from app.sandbox.paths import PathEscapeError, is_within, looks_absolute, resolve_under

# Page image naming: page-1.png, page-2.png, ...
PAGE_NAME_RE = re.compile(r"^page-(\d+)\.png$", re.IGNORECASE)


class PreviewService:
    """
    Page rasters live under ``preview_root / project_id / run_id / page-N.png``.

    Never writes preview images into the project workspace.
    """

    def __init__(self, store: ProjectStore, preview_root: Path) -> None:
        self.store = store
        self.preview_root = Path(preview_root).resolve()
        self.preview_root.mkdir(parents=True, exist_ok=True)

    def project_preview_dir(self, project_id: str) -> Path:
        # project_id validated by store
        self.store.get(project_id)
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise ProjectError(f"invalid project id: {project_id}", code="not_found")
        d = (self.preview_root / project_id).resolve()
        if not is_within(self.preview_root, d):
            raise ProjectError("preview path escapes preview root", code="path_escape")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def resolve_preview_file(self, project_id: str, relative: str) -> Path:
        """Resolve a path under this project's preview directory only."""
        if looks_absolute(relative):
            raise ProjectError(
                f"absolute paths are not allowed: {relative}", code="path_escape"
            )
        base = self.project_preview_dir(project_id)
        try:
            path = resolve_under(base, relative)
        except PathEscapeError as exc:
            raise ProjectError(str(exc), code="path_escape") from exc
        if not is_within(self.preview_root, path):
            raise ProjectError("preview path escapes preview root", code="path_escape")
        if not is_within(base, path):
            raise ProjectError("preview path escapes project preview dir", code="path_escape")
        return path

    def render_pdf_pages(
        self,
        project_id: str,
        pdf_rel: str = "main.pdf",
        *,
        dpi: int = 72,
        max_pages: int = 50,
    ) -> dict[str, Any]:
        """
        Rasterize project PDF pages into preview root.

        Source PDF is read from the project (sandbox); outputs go only under
        ``preview_root``.
        """
        pdf_path = self.store.resolve_file(project_id, pdf_rel)
        if not pdf_path.is_file():
            raise ProjectError(f"pdf not found: {pdf_rel}", code="file_not_found")
        if pdf_path.suffix.lower() != ".pdf":
            raise ProjectError("not a pdf file", code="invalid_pdf")

        # Ensure we never target project dir for output
        project_dir = self.store.projects_root / project_id
        run_id = uuid.uuid4().hex[:16]
        out_dir = self.resolve_preview_file(project_id, run_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        if is_within(project_dir.resolve(), out_dir):
            # Preview root must not be inside project (misconfiguration)
            raise ProjectError(
                "preview root must not be inside project workspace",
                code="preview_config",
            )

        pages = self._rasterize(pdf_path, out_dir, dpi=dpi, max_pages=max_pages)
        page_urls = [
            f"/api/projects/{project_id}/previews/{run_id}/page-{i}.png"
            for i in range(1, len(pages) + 1)
        ]
        return {
            "project_id": project_id,
            "run_id": run_id,
            "pdf_path": pdf_rel,
            "page_count": len(pages),
            "pages": pages,
            "page_urls": page_urls,
            "preview_root_relative": f"{project_id}/{run_id}",
        }

    def _rasterize(
        self,
        pdf_path: Path,
        out_dir: Path,
        *,
        dpi: int,
        max_pages: int,
    ) -> list[str]:
        """Write page-N.png into out_dir; return list of filenames."""
        pdftoppm = shutil.which("pdftoppm")
        if pdftoppm:
            return self._rasterize_pdftoppm(
                pdf_path, out_dir, pdftoppm=pdftoppm, dpi=dpi, max_pages=max_pages
            )
        return self._rasterize_pypdf_pillow(pdf_path, out_dir, max_pages=max_pages)

    def _rasterize_pdftoppm(
        self,
        pdf_path: Path,
        out_dir: Path,
        *,
        pdftoppm: str,
        dpi: int,
        max_pages: int,
    ) -> list[str]:
        prefix = out_dir / "page"
        # pdftoppm writes page-1.png when -png and prefix ends with page
        # Actually: pdftoppm -png -r 72 -f 1 -l N in.pdf outdir/page
        # produces page-1.png, page-2.png
        cmd = [
            pdftoppm,
            "-png",
            "-r",
            str(dpi),
            "-f",
            "1",
            "-l",
            str(max_pages),
            str(pdf_path),
            str(prefix),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            raise ProjectError(
                f"pdftoppm failed: {(proc.stderr or proc.stdout or '')[:500]}",
                code="render_failed",
            )
        return self._collect_pages(out_dir)

    def _rasterize_pypdf_pillow(
        self, pdf_path: Path, out_dir: Path, *, max_pages: int
    ) -> list[str]:
        """
        Fallback when pdftoppm is missing.

        pypdf does not render pages to bitmaps; we write a placeholder PNG per
        page count so the API shape stays testable without poppler. Prefer
        pdftoppm when available for real previews.
        """
        from pypdf import PdfReader
        from PIL import Image, ImageDraw

        reader = PdfReader(str(pdf_path))
        n = min(len(reader.pages), max_pages)
        if n < 1:
            n = 1
        names: list[str] = []
        for i in range(1, n + 1):
            img = Image.new("RGB", (400, 560), color=(245, 245, 245))
            draw = ImageDraw.Draw(img)
            draw.text((20, 20), f"Page {i} (fallback raster)", fill=(30, 30, 30))
            name = f"page-{i}.png"
            img.save(out_dir / name, format="PNG")
            names.append(name)
        return names

    def _collect_pages(self, out_dir: Path) -> list[str]:
        found: list[tuple[int, str]] = []
        for p in out_dir.iterdir():
            if not p.is_file():
                continue
            m = PAGE_NAME_RE.match(p.name)
            if m:
                found.append((int(m.group(1)), p.name))
        found.sort(key=lambda t: t[0])
        return [name for _, name in found]

    def list_run_pages(self, project_id: str, run_id: str) -> list[str]:
        if not re.fullmatch(r"[0-9a-f]{16}", run_id) and not re.fullmatch(
            r"[0-9a-f]{32}", run_id
        ):
            # allow 16-hex run ids we generate
            if not re.fullmatch(r"[0-9a-f]+", run_id):
                raise ProjectError(f"invalid run id: {run_id}", code="not_found")
        run_dir = self.resolve_preview_file(project_id, run_id)
        if not run_dir.is_dir():
            raise ProjectError(f"preview run not found: {run_id}", code="not_found")
        return self._collect_pages(run_dir)

    def get_page_path(self, project_id: str, run_id: str, page: int) -> Path:
        if page < 1:
            raise ProjectError("page must be >= 1", code="invalid_page")
        name = f"page-{page}.png"
        path = self.resolve_preview_file(project_id, f"{run_id}/{name}")
        if not path.is_file():
            raise ProjectError(f"page not found: {page}", code="not_found")
        return path
