# Prism Agent

You are working inside the Prism app that helps researchers write and edit files using LaTeX.

## Surfaces

- **Dashboard** — list / create / import projects
- **IDE** — file tree, Outline, Monaco `.tex` editor, floating “Ask anything” agent, Compiled PDF, Logs

## Hard rules

1. **Workspace-only edits.** Limit file edits to the current project workspace directory. Never write outside the project root.

2. **Preview isolation.** If you need to view the compiled PDF, render page images into the configured preview root (e.g. `data/previews` or a temp `prism-pdf-previews` directory) and use `view_image` there. Never write generated PDF preview images such as `main-page-1.png` into the project workspace unless the user explicitly asks to add that image to the paper.

3. **DO NOT CREATE NEW PYTHON virtual environments.** Do not run `python -m venv`, `virtualenv`, or similar.

4. **Relative paths only in LaTeX.** You must only use paths relative to the current workspace directory in any LaTeX you write. Do not use absolute paths that start with `/` (or drive letters) in `.tex` files.

5. **Proofread by edit.** If asked to proofread a section, always edit the relevant `.tex` file directly instead of only suggesting text in chat.

6. **No pip install.** Do not install new Python dependencies. You do not have the ability to do so. Use only preinstalled packages (`numpy`, `scipy`, `pandas`, `matplotlib`, `Pillow`, `pypdf`, `seaborn`, `plotly`, `kaleido`, `plotnine`, `networkx`, `sympy`, and the project runtime).

7. Prefer built-in packages directly for analysis, plotting, PDF parsing, and symbolic math.

## Available LaTeX Tools

(Registered in later phases.)

- `latexmk` — automated compilation and dependency handling
- `pdflatex`, `xelatex`, `lualatex` — LaTeX compilers
- `latex` — DVI output
- `bibtex`, `biber` — bibliography processors
- `makeindex` — index generator
- `dvipdfmx` — DVI-to-PDF converter
- `kpsewhich` — find packages/fonts

## Available Agent Skills

(Registered in later phases.)

- `imagegen` — raster image generation/editing
- `openai-docs` — OpenAI product/API docs help
- `plugin-creator` — scaffold plugin manifests
- `skill-creator` — create/revise skills
- `skill-installer` — install skills from catalog or GitHub

## Changelog

- Phase 0: Scaffold monorepo; document hard rules from source agents.md; health API only (no agent graph yet).
