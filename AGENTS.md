# Prism Agent

You are working inside the Prism app that helps researchers write and edit files using LaTeX.

## Surfaces

- **Dashboard** — list / create / import projects
- **IDE** — file tree, Outline, Monaco `.tex` editor, floating “Ask anything” agent, Compiled PDF, Logs

## Shipping rule (OpenPrism)

- **Canonical remote:** [https://github.com/nvmaditya/OpenPrism](https://github.com/nvmaditya/OpenPrism) (`origin` / `main`).
- **You must push every 100% tested feature** to that repo. Do not leave a completed, fully tested feature only on the local machine.
- Definition of “100% tested”: automated tests (or documented smoke checks) pass for the slice’s acceptance criteria.

### Required sequence after a feature (automatic via Grok hooks)

1. **Implement** the feature.
2. **Test** until green (`pytest` preferred — a successful run marks the ship gate).
3. On **turn end**, project hooks **auto-ship**:
   - stamp `docs/SHIPLOG.md` + `PLAN.md` last-ship line
   - `git commit` if dirty
   - `git push origin main` if ahead
4. If tests or push fail, the **Stop** hook blocks and you must fix, then finish again.

Still prefer meaningful commits and agent-facing `AGENTS.md` updates when the surface changes; the hook guarantees a docs stamp + remote sync even if you forget.

Hooks: `.grok/hooks/feature-ship.json` → `bin/auto_ship.py`. Trust with `/hooks-trust`; reload with `/hooks` → `r`.

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
| `groq` (default) | `ChatOpenAI` → `https://api.groq.com/openai/v1` using **`GROQ_API_KEY`** |
| `openai` / `custom` | `ChatOpenAI` (+ optional `PRISM_LLM_BASE_URL`); falls back to `GROQ_API_KEY` if OpenAI key empty |
| `ollama` | `ChatOpenAI` OpenAI-compatible; default base `http://127.0.0.1:11434/v1` |
| `azure` | `AzureChatOpenAI`; `PRISM_LLM_MODEL` = deployment; `PRISM_LLM_BASE_URL` = endpoint |
| `anthropic` | `ChatAnthropic` |

Also: `PRISM_LLM_MODEL` (Groq default `llama-3.3-70b-versatile`), `PRISM_LLM_API_KEY`, `PRISM_LLM_BASE_URL`, **`GROQ_API_KEY`** (from monorepo `.env`).

### Agent graph (`app.agent.graph.run_agent` / `build_agent`)

- LangGraph ReAct agent bound to a **project id**.
- FS tools (real `ProjectStore` + path sandbox):
  - `read_project_file(path)`
  - `write_project_file(path, content)`
  - `list_project_files()`
- Inject a chat model for tests (e.g. scripted tool-calling model); production uses `get_chat_model(settings)`.
- **Streaming chat (Phase 7):** `POST /api/projects/{id}/chat` with `{"message":"..."}` returns SSE (`text/event-stream`). Events: `token`, `tool_start`, `tool_end`, `file_changed`, `error`, `done`. FS writes surface as `file_changed` and land under that project only.

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

## Skills system (Phase 6)

Progressive `SKILL.md` packages. Catalog ships with the app; installs land under `PRISM_SKILL_HOME`.

### Catalog (five)

| Skill | Role |
|-------|------|
| `imagegen` | Raster image generation/editing guidance |
| `openai-docs` | Official OpenAI product/API guidance |
| `plugin-creator` | Scaffold `.codex-plugin/plugin.json` |
| `skill-creator` | Author/revise skill packages |
| `skill-installer` | Install catalog skills into skill-home |

### APIs

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/skills` | List catalog + skill-home (home overrides name clashes) |
| `GET` | `/api/skills/{name}` | Load full body + frontmatter |
| `POST` | `/api/skills/install` | `{ "name" }` copy catalog → skill-home |
| `POST` | `/api/skills/create` | skill-creator: write new `SKILL.md` under skill-home |
| `POST` | `/api/skills/plugin-scaffold` | plugin-creator: `.codex-plugin/plugin.json` under project or skill-home/plugins |

Live imagegen/GitHub install require credentials and are optional; list/load/local install/create/scaffold are the core platform.

## Changelog

- Phase 0: Scaffold monorepo; document hard rules from source agents.md; health API only (no agent graph yet).
- Phase 8 UI: Next dashboard + project IDE for feature verification; Groq via `GROQ_API_KEY`.
- Shipping: require push of every 100% tested feature to github.com/nvmaditya/OpenPrism.
- Phase 1: Multi-project domain — create/list/delete blank+example, zip import with zip-slip rejection, cross-project isolation, file read/write under project root.
- Phase 2: Structured workspace tree + multipart upload; hardened path sandbox on tree/read/write/upload (`../` and absolute rejected).
- Phase 3: Provider-pluggable `get_chat_model` (openai/azure/ollama/anthropic/custom) + LangGraph agent with project-scoped FS tools; mock-driven tests.
- Phase 4: Allowlisted TeX subprocess runner; project compile + retrievable logs (`latexmk`/`pdflatex`/…).
- Phase 5: PDF serve (`/pdf`) + page previews under isolated `PRISM_PREVIEW_ROOT` (pdftoppm or Pillow fallback).
- Phase 6: Progressive skills — five catalog packages, list/load/install/create, plugin scaffold; skill-home via `PRISM_SKILL_HOME`.

