# Prism Agent

You are working inside the Prism app that helps researchers write and edit files using LaTeX.

## Surfaces

- **Dashboard** — list / create / import projects
- **IDE** — file tree, Outline, Monaco `.tex` editor, floating “Ask anything” agent, Compiled PDF, Logs

## Shipping rule (OpenPrism)

- **Canonical remote:** [https://github.com/nvmaditya/OpenPrism](https://github.com/nvmaditya/OpenPrism) (`origin` / `main`).
- **You must push every 100% tested feature** to that repo. Do not leave a completed, fully tested feature only on the local machine.
- Definition of “100% tested” for a feature slice: automated tests (or documented smoke checks for UI) pass for that slice’s acceptance criteria, and the change is committed with a clear message.
- After tests pass: `git push -u origin main` (or push the feature branch + open PR, then merge to `main` if not on main). Confirm the remote has the commit before claiming the feature is done.
- Do **not** push half-baked or failing work; fix tests first, then push.

## Hard rules

1. **Workspace-only edits.** Limit file edits to the current project workspace directory. Never write outside the project root.

2. **Preview isolation.** If you need to view the compiled PDF, render page images into the configured preview root (e.g. `data/previews` or a temp `prism-pdf-previews` directory) and use `view_image` there. Never write generated PDF preview images such as `main-page-1.png` into the project workspace unless the user explicitly asks to add that image to the paper.

3. **DO NOT CREATE NEW PYTHON virtual environments.** Do not run `python -m venv`, `virtualenv`, or similar.

4. **Relative paths only in LaTeX.** You must only use paths relative to the current workspace directory in any LaTeX you write. Do not use absolute paths that start with `/` (or drive letters) in `.tex` files.

5. **Proofread by edit.** If asked to proofread a section, always edit the relevant `.tex` file directly instead of only suggesting text in chat.

6. **No pip install.** Do not install new Python dependencies. You do not have the ability to do so. Use only preinstalled packages (`numpy`, `scipy`, `pandas`, `matplotlib`, `Pillow`, `pypdf`, `seaborn`, `plotly`, `kaleido`, `plotnine`, `networkx`, `sympy`, and the project runtime).

7. Prefer built-in packages directly for analysis, plotting, PDF parsing, and symbolic math.

## Project & FS APIs (Phase 1–2)

- `GET/POST /api/projects` — list / create (`kind`: `blank` | `example`)
- `POST /api/projects/import` — zip import (zip-slip rejected)
- `GET/DELETE /api/projects/{id}`
- `GET /api/projects/{id}/files` — flat file path list
- `GET /api/projects/{id}/tree` — **structured** directory tree (dirs + files + sizes); optional `?path=` subdir
- `POST /api/projects/{id}/upload` — multipart upload (`file`, optional form `path` dest dir/file); default `prism-uploads/<filename>`
- `GET/PUT /api/projects/{id}/files/{path}` — read/write project files (text JSON or base64 for binary)

### FS sandbox (agents must respect; server enforces)

- All paths are **relative to the project root** only.
- Reject `..` segments and absolute paths (`/…`, `C:\…`); never write outside the project storage root.
- Prefer `prism-uploads/` for user assets (images, etc.).

## LLM providers + agent (Phase 3)

### Provider factory (`app.agent.providers.get_chat_model`)

Switch backend via env only (no source edits):

| `PRISM_LLM_PROVIDER` | Construction |
|----------------------|--------------|
| `openai` / `custom` | `ChatOpenAI` (+ optional `PRISM_LLM_BASE_URL`) |
| `ollama` | `ChatOpenAI` OpenAI-compatible; default base `http://127.0.0.1:11434/v1` |
| `azure` | `AzureChatOpenAI`; `PRISM_LLM_MODEL` = deployment; `PRISM_LLM_BASE_URL` = endpoint |
| `anthropic` | `ChatAnthropic` |

Also: `PRISM_LLM_MODEL`, `PRISM_LLM_API_KEY`, `PRISM_LLM_BASE_URL`.

### Agent graph (`app.agent.graph.run_agent` / `build_agent`)

- LangGraph ReAct agent bound to a **project id**.
- FS tools (real `ProjectStore` + path sandbox):
  - `read_project_file(path)`
  - `write_project_file(path, content)`
  - `list_project_files()`
- Inject a chat model for tests (e.g. scripted tool-calling model); production uses `get_chat_model(settings)`.
- SSE/HTTP chat streaming is a later phase; this phase is the invoke entry + tools.

## LaTeX tools + compile (Phase 4)

Allowlisted binaries only (server rejects others):  
`latexmk`, `pdflatex`, `xelatex`, `lualatex`, `latex`, `bibtex`, `biber`, `makeindex`, `dvipdfmx`, `kpsewhich`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/projects/tools` | List allowlisted tool names |
| `POST` | `/api/projects/{id}/compile` | Compile `main_file` with `engine` (default `latexmk`); returns `log_id`, `success`, `log`, optional `pdf_path` |
| `POST` | `/api/projects/{id}/tools/run` | Run one allowlisted tool with `args` in project cwd |
| `GET` | `/api/projects/{id}/logs` | List compile/tool run metas |
| `GET` | `/api/projects/{id}/logs/{log_id}` | Fetch full log text + meta |

- Subprocess **cwd** = project root; no shell; bare tool names only (no path-like tool).
- Logs stored under project `.prism/logs/{log_id}.log` (+ `.json` meta), still inside the project workspace.
- Prefer `latexmk -pdf` for multi-pass docs; `pdflatex` for simple PDF builds.
- Compile response includes `log_id`, `log_url`, `pdf_path`, `pdf_available`, `pdf_url` when a PDF is produced.

## PDF serve + previews (Phase 5)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/projects/{id}/pdf?path=main.pdf` | Serve compiled PDF bytes (`application/pdf`) from project workspace |
| `POST` | `/api/projects/{id}/previews/render` | Rasterize pages into **preview root** only; returns `run_id`, `page_urls` |
| `GET` | `/api/projects/{id}/previews/{run_id}` | List page images for a render run |
| `GET` | `/api/projects/{id}/previews/{run_id}/page-N.png` | Fetch one page PNG |

### Preview isolation (hard rule)

- Page images (`page-1.png`, …) are written only under `PRISM_PREVIEW_ROOT/{project_id}/{run_id}/`.
- **Never** write generated PDF preview images into the project paper tree by default.
- Use `view_image` / preview URLs from the preview root, not from the project workspace.

## Available Agent Skills

(Registered in later phases.)

- `imagegen` — raster image generation/editing
- `openai-docs` — OpenAI product/API docs help
- `plugin-creator` — scaffold plugin manifests
- `skill-creator` — create/revise skills
- `skill-installer` — install skills from catalog or GitHub

## Changelog

- Phase 0: Scaffold monorepo; document hard rules from source agents.md; health API only (no agent graph yet).
- Shipping: require push of every 100% tested feature to github.com/nvmaditya/OpenPrism.
- Phase 1: Multi-project domain — create/list/delete blank+example, zip import with zip-slip rejection, cross-project isolation, file read/write under project root.
- Phase 2: Structured workspace tree + multipart upload; hardened path sandbox on tree/read/write/upload (`../` and absolute rejected).
- Phase 3: Provider-pluggable `get_chat_model` (openai/azure/ollama/anthropic/custom) + LangGraph agent with project-scoped FS tools; mock-driven tests.
- Phase 4: Allowlisted TeX subprocess runner; project compile + retrievable logs (`latexmk`/`pdflatex`/…).
- Phase 5: PDF serve (`/pdf`) + page previews under isolated `PRISM_PREVIEW_ROOT` (pdftoppm or Pillow fallback).

