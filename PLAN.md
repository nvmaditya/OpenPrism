> **Last auto-ship:** 2026-08-08 06:24 UTC · docs/SHIPLOG.md

# Prism Reverse-Engineer — Implementation Plan

> **Status:** Phases **0–9 complete** on https://github.com/nvmaditya/OpenPrism · **Next: Phase 10** (harden and ship)  
> **Living plan:** `C:\Code\prism\PLAN.md` (source of truth for phase map)  
> **Updated:** 2026-08-07 · Phase 8 redefined as end-user Prism UX + abstraction; old Phase 9 ship split into Phase 9 + Phase 10

> **For agentic workers:** Implement phase-by-phase using skills-guide loop (`Think → Plan → Implement → Verify → Review → Next`). Prefer `subagent-driven-development` or `executing-plans` per phase. Hard rules use TDD. Push 100% tested features to OpenPrism; do not drive-by rewrite docs “for the push.”

**Goal:** Reverse-engineer **OpenAI Prism** (`https://prism.openai.com`) as a local full-stack app at `C:\Code\prism`: **LangGraph agent + FastAPI + Next.js**, matching the observed product flow and the agent/tools/skills docs in `Downloads/LaTEX`.

**Architecture:** Single-user local monorepo with **many projects** on disk. FastAPI owns project FS, TeX compile, skill registry, LangGraph agent, SSE chat. Next.js is an **end-user presentation layer** over those APIs: **Your Projects** dashboard + project **IDE** (Files, editor, floating Ask anything, Compiled preview first, Logs). API DTOs (ids, log_id, SSE events) stay off primary chrome. Skills are progressive `SKILL.md` packages; LLM is **provider-pluggable** (default **Groq** via `GROQ_API_KEY`).

**Tech stack:**
- Backend: Python 3.12+, FastAPI, Uvicorn, LangChain, LangGraph, pydantic-settings
- Frontend: Next.js (App Router), TypeScript, Tailwind (product chrome first; Monaco optional later)
- Runtime: TeX Live / MiKTeX on host; Groq/OpenAI-compatible chat; optional images API for `imagegen`
- **Sources of truth:**
  - UI/flow: `C:\Users\chhav\Downloads\LaTEX\PrismFlow\*.png` + product screenshots
  - Agent rules: root `AGENTS.md` (from LaTEX agents)
  - Skills/tools: backend catalog + LaTEX skills/tools notes
  - Build process: [skills-guide](https://github.com/nvmaditya/skills-guide)

**Locked decisions:**
| Decision | Choice |
|----------|--------|
| Location | `C:\Code\prism` monorepo → GitHub `nvmaditya/OpenPrism` |
| Skill surface | **Full original** (imagegen, openai-docs, plugin-creator, skill-creator, skill-installer) |
| LLM | Provider-pluggable; default **groq** + `GROQ_API_KEY` |
| Auth | Single-user local first (Invite/Shared stubbed) |
| Frontend Phase 8 | **End-user abstraction**, not a developer “verify console” |

---

## 0. Product reverse-engineer (from PrismFlow screenshots)

Observed product: **Prism** at `prism.openai.com` — dark IDE for LaTeX projects with live compile + bottom chat agent.

### 0.1 Surface A — Project dashboard

From screenshots `130915`, `130932`, `130942`:

| UI element | Behavior to clone |
|------------|-------------------|
| Left nav | **All Projects**, **Your Projects**, **Shared with you** |
| User chip | Bottom-left profile (local: single user display name) |
| Project list | Name + relative created time (`19h`, `3mo`); row `…` menu |
| Search | Filter projects by name |
| View toggles | List / grid |
| **Import ▾** | Import archive (`.zip`, `.tar.gz`); Import Folder |
| **+ New ▾** | Blank project; Example project; New folder |

**Local single-user mapping:** “Shared with you” can stay empty or hidden; projects live under `data/projects/{id}/`. Import/New are first-class APIs.

### 0.2 Surface B — Project IDE

From screenshots `131011`, `131023`:

```
┌─────────────┬──────────────────────────────┬──────────────────────────┐
│ Project ▾   │  main.tex tab          Close │  Tools | Compiled ▾      │
│ Invite      │  ─────────────────────────── │  Zoom | ⋮ | Go to PDF    │
│ Files|Chats │  Monaco LaTeX editor         │  PDF page preview        │
│ file tree   │                              │  ── or ──                │
│ Outline     │                              │  Project Info|Versions|  │
│             │  ╔ Ask anything ══════════╗  │  Comments|Logs           │
│ user chip   │  ║ model ▾  +  🎤  send   ║  │  raw TeX logs            │
└─────────────┴──────────────────────────────┴──────────────────────────┘
```

| Region | Observed details | Implementation notes |
|--------|------------------|----------------------|
| **Left: Files** | Folder `prism-uploads`, `main.tex`, `profile.jpg`; search; `+` add | Workspace tree API; upload assets into project |
| **Left: Chats** | Tab next to Files | Per-project conversation list + active thread |
| **Outline** | Section titles from LaTeX (`PROFESSIONAL SUMMARY`, …) | Parse `\section` / `\section*` (and resume macros) → clickable jump-to-line |
| **Center editor** | Syntax-colored `.tex`; tab + Close | Monaco + multi-tab later; v1 single open file OK |
| **Floating chat** | Placeholder **“Ask anything”**; model **“5.6 Sol Extra high”**; attach `+`; voice; send | SSE agent; model dropdown = provider models; voice optional late; attach = file context |
| **Right: Compiled** | Live PDF of resume/paper; zoom; page prev/next; **Go to PDF** | Compile pipeline → PDF serve + page images; open full PDF |
| **Right: Logs** | Raw `pdflatex` / TeX Live logs | Capture subprocess stdout/stderr; show in Logs tab |
| **Right: Tools / Project Info / Versions / Comments** | Present in chrome | **Tools** = compile actions / agent tools panel; Versions/Comments = Phase 9 stubs |

### 0.3 End-to-end user journeys (must work)

```text
J1 Dashboard
  Open app → list projects → New (Blank | Example) | Import zip/folder → open project

J2 Edit + compile
  Open main.tex → edit → compile (manual Tools or agent) → Compiled PDF updates → Logs show TeX output

J3 Agent edit
  “Ask anything” → agent reads/writes .tex in project only → optional recompile → preview refresh → proofread changes are file edits not chat-only suggestions

J4 Outline nav
  Click Outline section → editor scrolls to line

J5 Assets
  Upload image (e.g. profile.jpg) under project / prism-uploads → reference from LaTeX with relative path
```

### 0.4 Agent policy (from `agents.md` — still binding)

1. Limit file edits to **project** workspace  
2. PDF **page previews** only under preview root (not into paper tree unless user asks)  
3. **No new Python venvs**  
4. **Relative paths only** in agent-written LaTeX (no absolute `/...`)  
5. Proofread → **edit `.tex` directly**  
6. Preinstalled science packages only; **no pip install**  

### 0.5 Tools surface (from `tools.md`)

`latexmk`, `pdflatex`, `xelatex`, `lualatex`, `latex`, `bibtex`, `biber`, `makeindex`, `dvipdfmx`, `kpsewhich`  
+ FS: read/write/edit/list/upload  
+ `view_image` (preview root)  
+ restricted `run_python`

### 0.6 Skills surface (from `skills.md` — full v1)

`imagegen`, `openai-docs`, `plugin-creator`, `skill-creator`, `skill-installer`

---

## 1. Skills-guide build flow (how we implement)

Default loop every phase:

```
Think → Plan → Implement (small) → Verify → Review → Next
```

| Phase | Milestone | Primary skills | Hard rule / acceptance |
|-------|-----------|----------------|------------------------|
| 0 | Monorepo + `AGENTS.md` + `PLAN.md` | orient | Docs match PrismFlow + LaTEX sources |
| 1 | Projects domain (CRUD, import) | `tdd`, `ponytail` | Project isolation on disk |
| 2 | Sandbox FS + file API | `tdd` | Path escape rejected |
| 3 | Provider factory + LangGraph | `tdd` | Provider switch via env |
| 4 | TeX tools + Logs capture | domain, `tdd` | Fixture compiles; logs stored |
| 5 | PDF Compiled panel pipeline | `tdd` | Previews outside project tree |
| 6 | Skills full surface | `tdd` | All 5 skills load/install |
| 7 | FastAPI SSE chat + project APIs | `tdd` | SSE tokens + tool events + file_changed |
| 8 | End-user Prism UX + abstraction (dashboard + IDE) | structure + hide debug chrome | J1–J3, J5 usable without raw API/SSE literacy |
| 9 | Outline, import polish, stubs | product chrome | Outline jump + import |
| 10 | Harden and ship | verify | Fresh clone + guardrails |

**UI rule (skills-guide):** structure and states first; taste skills only after IDE chrome works.

**Living docs:**
- `AGENTS.md` — agent policy + tools/skills inventory (update every surface change)
- `PLAN.md` — this plan, checkboxes marked done
- Link skills-guide; do not fork it into the repo

---

## 2. Monorepo layout

```text
C:\Code\prism\
  AGENTS.md
  PLAN.md
  README.md
  .env.example
  backend/
    pyproject.toml
    app/
      main.py
      settings.py
      api/
        projects.py       # list/create/import/delete
        files.py          # tree, read, write, upload
        chat.py           # SSE agent
        compile.py        # compile + logs + pdf
        previews.py
        skills.py
      domain/
        project.py        # Project model, paths
      agent/
        graph.py
        state.py
        prompts.py        # inject AGENTS.md
        providers.py      # pluggable LLM
        tools/
          fs.py
          latex.py
          python_exec.py
          image_view.py
          skill_tools.py
      skills/
        loader.py
        registry.py
        catalog/
          imagegen/SKILL.md
          openai-docs/SKILL.md
          plugin-creator/SKILL.md
          skill-creator/SKILL.md
          skill-installer/SKILL.md
      sandbox/
        paths.py
        process.py
      compile/
        engine.py         # latexmk etc + log capture
        pdf_render.py     # pages for preview strip if needed
      outline/
        parse_tex.py      # section outline
    tests/
      fixtures/
        sample_paper/
        example_resume/   # mirrors Prism example project shape
  frontend/
    app/
      page.tsx              # dashboard (Your Projects)
      project/[id]/page.tsx # IDE
    components/
      dashboard/
        ProjectList.tsx
        ImportMenu.tsx
        NewProjectMenu.tsx
      ide/
        FileTree.tsx
        Outline.tsx
        TexEditor.tsx
        AskAnything.tsx     # floating chat bar
        CompiledPreview.tsx
        LogsPanel.tsx
        RightRail.tsx       # tabs: Project Info | Versions | Comments | Logs
        LeftRail.tsx        # Files | Chats
    lib/api.ts
  data/
    projects/               # {project_id}/ files
    previews/               # compile page images (not inside project)
    skill-home/
    chats/                  # optional SQLite/JSON threads
```

---

## 3. Backend design

### 3.1 Project model

```python
# Project = isolated workspace root
# data/projects/{id}/
#   main.tex (or multi-file)
#   prism-uploads/
#   .prism/meta.json   # name, created_at, example_flag
```

APIs:
- `GET /api/projects` — list (Your Projects)
- `POST /api/projects` — body: `{ kind: blank|example|folder }`
- `POST /api/projects/import` — multipart zip/tar.gz or folder upload
- `DELETE /api/projects/{id}`
- `GET/PUT /api/projects/{id}/files/...`
- `GET /api/projects/{id}/outline`
- `POST /api/projects/{id}/compile` → `{ pdf_url, log_id, pages[] }`
- `GET /api/projects/{id}/logs/{log_id}`
- `POST /api/projects/{id}/chat` SSE
- skills endpoints under `/api/skills`

### 3.2 LangGraph agent

```
START → agent ⇄ tools → END
```

State: `messages`, `project_id`, `loaded_skills`, `file_events`, `last_compile`.

System prompt = `AGENTS.md` + project path conventions + short skill catalog.

Streaming events to UI:

| event | purpose in IDE |
|-------|----------------|
| `token` | chat transcript / streaming answer |
| `tool_start` / `tool_end` | optional Tools activity |
| `file_changed` | refresh tree + dirty editor if open file changed |
| `compile_finished` | refresh Compiled + Logs |
| `preview_ready` | page images if used |
| `done` / `error` | terminal |

### 3.3 Provider factory

`PRISM_LLM_PROVIDER` ∈ `openai | azure | ollama | anthropic | custom`  
+ `PRISM_LLM_MODEL`, `PRISM_LLM_API_KEY`, `PRISM_LLM_BASE_URL`  

Frontend model dropdown lists configured models from `GET /api/models`.

### 3.4 Compile + Logs + Compiled

1. Run allowlisted TeX tool (`latexmk -pdf` default) in project cwd  
2. Persist raw log → Logs tab  
3. Serve PDF at `/api/projects/{id}/pdf`  
4. Optional page raster under `data/previews/{id}/` for strip/zoom helpers  
5. Never write `main-page-1.png` into project unless user asks (agents.md rule)

### 3.5 Outline

`parse_tex.py`: regex/heuristic for `\section`, `\section*`, `\subsection`, custom resume macros → `{ title, line, level }[]`.  
API + left Outline panel click → editor `revealLine`.

---

## 4. Frontend design (match Prism chrome)

### 4.1 Dashboard (`/`)

- Sidebar: All / Your Projects / Shared (stub)
- Main: searchable project table
- Import ▾ + New ▾ menus (exact actions from screenshots)

### 4.2 IDE (`/project/[id]`)

- **LeftRail:** Files | Chats; tree; Outline; user chip  
- **Center:** Monaco; file tab; Close  
- **AskAnything:** fixed bottom-center bar — model select, attach, send (voice later)  
- **RightRail:** Tools button; Compiled viewer **or** tabbed Project Info / Versions / Comments / Logs  

Visual: dark theme default to match Prism; polish later with taste skill if desired.

### 4.3 Chats tab

- List threads per project  
- Selecting a chat loads history into AskAnything transcript area (expandable panel above input if needed)  
- v1: one default thread per project is enough; multi-thread in same phase if cheap

---

## 5. Phases (detailed)

### Phase 0 — Scaffold + living docs ✅

**Do:**
1. Create `C:\Code\prism`, git init  
2. Write `AGENTS.md` from LaTEX agents + note UI surfaces from PrismFlow  
3. Copy approved plan → `PLAN.md`  
4. Backend health; frontend blank shell  
5. `.env.example`  

**Exit:** health OK; `AGENTS.md` committed.  

**Update AGENTS.md:** identity + hard rules.

---

### Phase 1 — Projects domain ✅

**Do:** create/list/delete blank + example project; import zip; disk layout under `data/projects`.  

**Tests:** isolation between two project ids; zip extract stays inside project root (zip-slip rejected).  

**Exit:** API-only J1 works via curl/httpx.

---

### Phase 2 — Sandbox FS + file API ✅

**Do:** tree, read, write, upload; path sandbox.  

**Hard tests:** `../` escape, absolute escape, zip-slip.  

**Exit:** files appear for IDE later.

**Update AGENTS.md:** FS tools.

---

### Phase 3 — Providers + LangGraph skeleton ✅

**Do:** `get_chat_model`; graph with FS tools bound to `project_id`; mock tests.  

**Exit:** agent can read/write a fixture project with mock LLM tool calls.

---

### Phase 4 — TeX tools + log capture ✅

**Do:** wrap full tools.md allowlist; `compile.engine`; store logs.  

**Exit:** fixture/example resume builds when TeX installed; log text retrievable.

**Update AGENTS.md:** tool list.

---

### Phase 5 — Compiled PDF pipeline ✅

**Do:** PDF endpoint; optional page previews under preview root; enforce no workspace pollution.  

**Exit:** tests on preview path; compile returns pdf_url + log_id.

---

### Phase 6 — Skills (full surface) ✅

**Do:** registry, load_skill, five catalog packages from skills.md; installer → skill-home; creator/plugin scaffolds; imagegen + openai-docs tools (keys optional, tests skippable).  

**Exit:** list/load/install tests green.

**Update AGENTS.md:** skills section.

---

### Phase 7 — Streaming chat API ✅

**Do:** `POST .../chat` SSE with full event set; wire compile_finished after agent compile tool.  

**Exit:** automated SSE client test.

**Shipped:** `POST /api/projects/{id}/chat` · token / tool / file_changed / done · scripted-model tests.

---

### Phase 8 — End-user Prism UX + presentation abstraction ✅

**Product goal (supersedes interim “verify console” structure UI):**  
Ship a **Prism-like end-user** dashboard + project editor. Hide developer/debug chrome by default (no API health URL, hex project ids as primary labels, SSE/role dumps, raw `success=` compile telemetry, or skills-catalog dump on the main dashboard).

**Do:**
- **Your Projects** list with **Blank** / **Example** create; open into a project by name  
- Project workspace: **Files**, editable source, floating **Ask anything**, right rail **Compiled** (document preview first) + **Logs** (human success/failure; technical log behind progressive disclosure)  
- Keep real APIs underneath (projects, files, compile/PDF, SSE chat) — map DTOs to user-facing labels only  

**Exit:** banned debug strings absent from primary chrome; create/edit/save/compile/chat still hit backend; journeys J1–J3 + J5 usable without developer literacy.

**Shipped:** `frontend/app/page.tsx` + `frontend/app/project/[id]/page.tsx` · presentation layer over existing APIs · PLAN phase map 8→10.

---

### Phase 9 — Outline, import polish, stubs (next)

**Status:** done  

**Do:**
- Document outline jump (LaTeX sections → editor line)  
- Import zip/folder UX if not already first-class in UI  
- Versions / Comments / Project Info minimal stubs  
- Invite/Shared disabled with single-user local affordance  

**Exit:** outline + import happy path; stubs present without multi-tenant auth.

**Shipped:** `GET /api/projects/{id}/outline` · dashboard zip Import · IDE Outline jump · Project Info / Versions / Comments stubs · Invite/Shared local-only.

---

### Phase 10 — Harden and ship

**Status:** pending  

**Do:**
- Guardrail tests (pip, path lint, preview root)  
- README quickstart (TeX, `GROQ_API_KEY`, `./start.sh`)  
- Optional Playwright for compile + chat journeys  
- `verification-before-completion` / `check-work`  

**Exit:** fresh clone path works; product UI remains abstraction-first.

---

## 6. Hard rules matrix

| Rule | Layer | Test |
|------|-------|------|
| Project-only edits | `sandbox/paths.py` | traversal + cross-project |
| Zip-slip safe import | import handler | malicious zip |
| Previews outside project | `pdf_render` | path prefix |
| No venv / no pip | `python_exec` + prompt | rejected commands |
| Relative paths in agent tex | prompt + optional lint | absolute path fail |
| Proofread edits files | prompt + E2E | file hash changes |
| Logs available after compile | compile engine | log non-empty on success/fail |

---

## 7. Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Clone **Prism UI flow**, not a generic chat app | Yes | PrismFlow screenshots define product |
| Multi-project dashboard even for single-user | Yes | Matches real Prism; local = many folders |
| Floating “Ask anything” not a separate chat-only page | Yes | Screenshot IDE pattern |
| Compiled + Logs first-class right rail | Yes | Core loop is write→compile→read log |
| Outline from LaTeX headings | Yes | Present in product; high leverage |
| Versions/Comments/Invite deferred stubs | Yes | Single-user first; keep chrome honest |
| Provider-pluggable LLM | Yes | User lock |
| Full skill surface in v1 | Yes | User lock |
| skills-guide for *build* process | Yes | Phase skill table; not product skills |
| Phase 8 = end-user abstraction (not verify console) | Yes | Hide API/SSE/log-id chrome; Prism-like product UI |
| LLM default Groq | Yes | `GROQ_API_KEY` in monorepo `.env` |

---

## 8. Open questions (defaults)

| Question | Default |
|----------|---------|
| Voice mic in Ask anything | Phase 9+ skip unless requested |
| Real-time collab / Invite | Out of v1 |
| Auto-compile on save | Optional flag; default manual or agent-triggered |
| Example project content | Ship a small resume/article fixture like Prism demo |
| Docker TeX | Not required for v1 |

---

## 9. PR slicing

1. PR0 Scaffold + AGENTS.md  
2. PR1 Projects + import  
3. PR2 Sandbox FS  
4. PR3 LangGraph + providers  
5. PR4 TeX + logs  
6. PR5 PDF compiled  
7. PR6 Skills  
8. PR7 SSE chat  
9. PR8 End-user Prism UX + abstraction  
10. PR9 Outline / import / stubs  
11. PR10 Harden + ship  

Each PR that changes agent surface updates **`AGENTS.md` changelog**.

---

## 10. First session after approval

1. `C:\Code\prism` scaffold  
2. `AGENTS.md` (rules + tools/skills placeholders + Prism surfaces note)  
3. `PLAN.md`  
4. Backend `/health` + frontend dashboard shell  
5. Start Phase 1 TDD (projects)

---

## 11. Non-goals (v1)

- Multi-tenant auth, billing, real Invite/Shared  
- Pixel-perfect Prism brand cloning (structure + dark IDE is enough)  
- Full OT multiplayer editor  
- Replacing system TeX with pure Python  
- Custom foundation model training  

---

## 12. Reference assets

| Path | Use |
|------|-----|
| `Downloads/LaTEX/PrismFlow/*.png` | UI/IA reverse-engineer |
| `Downloads/LaTEX/agents.md` | Agent hard rules |
| `Downloads/LaTEX/skills.md` | Skill bodies |
| `Downloads/LaTEX/tools.md` | TeX tool docs |
| `C:\Code\skills-guide` | When-to-use which build skill |

## Deviations

- Used uv-managed CPython 3.12 + project `.venv` because host only had Windows Store python stubs.
- Next.js from create-next-app; interim Phase 8 “verify console” UI was replaced by end-user Prism abstraction (same phase number, redefined product goal).
- Phase map extended: former single “ship” Phase 9 split into **Phase 9** (outline/import/stubs) + **Phase 10** (harden/ship).
- Default LLM provider set to **Groq** (`GROQ_API_KEY`) for local chat.

## Phase status summary

| Phase | Name | Status |
|-------|------|--------|
| 0 | Scaffold + living docs | ✅ done |
| 1 | Projects domain | ✅ done |
| 2 | Sandbox FS + file API | ✅ done |
| 3 | Providers + LangGraph | ✅ done |
| 4 | TeX tools + logs | ✅ done |
| 5 | PDF serve + previews | ✅ done |
| 6 | Skills full surface | ✅ done |
| 7 | SSE streaming chat | ✅ done |
| 8 | End-user Prism UX + abstraction | ✅ done |
| 9 | Outline, import polish, stubs | ✅ done |
| 10 | Harden and ship | ⏳ next |

### Phase 0 status

- [x] Scaffold complete at `C:\Code\prism` (backend /health, frontend shell, AGENTS.md, PLAN.md, .env.example).

### Phase 1 status

- [x] Projects domain: create/list/delete blank+example, zip import, zip-slip rejection, isolation tests; pushed to OpenPrism.

### Phase 2 status

- [x] Sandbox FS: structured tree, upload, path escape tests; pushed to OpenPrism.

### Phase 3 status

- [x] Provider factory + LangGraph FS agent skeleton; mock tests; pushed to OpenPrism.

### Phase 4 status

- [x] TeX allowlist runner + compile logs; pushed to OpenPrism.

### Phase 5 status

- [x] PDF serve + preview root isolation; pushed to OpenPrism.

### Phase 6 status

- [x] Skills catalog + install/create/plugin scaffold; pushed to OpenPrism.

### Phase 7 status

- [x] SSE chat stream (`POST /api/projects/{id}/chat`); token/tool/file_changed/done; scripted-model tests; pushed to OpenPrism.

### Phase 8 status

- [x] End-user dashboard (**Your Projects**, Blank/Example) + IDE (Files, editor, Ask anything, Compiled/Logs) without primary debug chrome; PLAN map 8→10; pushed to OpenPrism.
- [x] Resume Ask-anything: modular Example (`sections/` + `\input`), `add_tex_input`, host verify (static + compile + PDF text) with rollback, agent `trace_id` on Logs.

### Phase 9 status

- [x] Outline jump, import UX, Versions/Comments/Project Info stubs.

### Phase 10 status

- [ ] Harden, Playwright optional, ship checklist.

