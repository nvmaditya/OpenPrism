# AGENTS.md

Guide for coding agents and human contributors working in this repository. [CONTRIBUTING.md](CONTRIBUTING.md) is the full contributor guide; this file is the short version.

## What this is

PrismTeX is a self-hosted LaTeX workspace: a FastAPI backend that stores projects and compiles them with latexmk, and a Next.js UI. The AI writing agent is being rebuilt and returns in Phase 1 (v0.1.0); there is no agent code in the tree today.

## Repository map

- `backend/src/prismtex/`: the Python package.
  - `main.py`: `create_app(settings=None, *, compiler=None)`, the composition root. There is no module-level app.
  - `cli.py`, `__main__.py`: `prismtex serve`.
  - `settings.py`: `Settings`, the only reader of `PRISMTEX_*` configuration.
  - `layout.py`: `DataLayout`, every path under the data directory.
  - `errors.py`: the `AppError` hierarchy and the error envelope.
  - `security.py`: Host, access-token and Origin middleware.
  - `migrate.py`: one-time migration of legacy `.prism/` folders.
  - `cancel.py`: `CancelToken`, the cancellation flag a Turn polls and awaits.
  - `workspace/`: path policy, text versions and line endings, `ProjectStore`, ZIP import, project templates.
  - `outline/`: the Outline (headings with their spans, the `\input` graph, labels, citations and packages) and the per-file parser behind `GET /outline`.
  - `compile/`: latexmk argv and environment, the hardened runner, log parsing, `Compiler`, compile logs.
  - `api/`: FastAPI routers.
- `backend/tests/`: pytest. Tests marked `tex` need a real TeX installation.
- `frontend/`: the Next.js UI. `lib/api.ts` is the only API client.
- `docs/`: configuration, compile hardening, design documents, ADRs and phase specs.

## Commands

Setup, from the repository root: `python3 -m venv .venv && .venv/bin/python -m pip install -e 'backend[dev]'`, then `cd frontend && npm ci`. Run both servers with `./start.sh`, which picks up `.venv`. The venv is not optional on systems with an externally-managed Python (PEP 668: Ubuntu 24.04, Debian, Homebrew), where a bare `pip install` fails; on Windows Git Bash the interpreter is `.venv/Scripts/python`, and Debian and Ubuntu may need `apt install python3-venv`.

From `backend/`:

```bash
pytest -m "not tex"     # fast lane
pytest -m tex           # needs latexmk; fails, never skips, without it
ruff check src tests && ruff format --check src tests
pyright
```

From `frontend/`: `npm run lint`, `npm run typecheck`, `npm run build`.

A change is done when all of these pass.

## Conventions

- **Commits:** Conventional Commits (`feat(compile): ...`, `fix: ...`, `docs: ...`).
- **Line endings:** LF in every text file, UTF-8.
- **Review-first:** agent changes reach a project only as a proposal the user accepts hunk by hunk ([ADR 0003](docs/adr/0003-review-first-proposals.md)).
- **Project folders hold only user files:** app state, logs and build output go under the `DataLayout` paths `state/`, `build/` and `history/` ([ADR 0004](docs/adr/0004-project-folders-hold-only-user-files.md)).
- **Paths:** every path that comes from a request or a project goes through `workspace.paths` (`check_relative_path`, `resolve_in`).
- **Compile commands:** the latexmk argv and environment are built only in `prismtex.compile.argv`, from a validated root file and engine.
- **Errors:** raise an `AppError` subclass; every error response has the body `{"detail": {"code", "message", ...}}`.
- **The UI calls `/api/...` on its own origin only** ([ADR 0005](docs/adr/0005-same-origin-api-no-login-on-localhost.md)).
- **Tests** build `Settings(_env_file=None, data_dir=tmp_path, ...)` so a developer's `.env` never leaks in, and use `TestClient(app, base_url="http://127.0.0.1:8000")`, because the Host check rejects `testserver`.
- **Dependencies:** a new runtime dependency needs an issue and agreement first.
- **Docs describe what exists.** Update them in the same change as the code.
- **Words:** use the terms in [CONTEXT.md](CONTEXT.md).

## Design documents

Before changing how an area behaves, read its section in these:

- [docs/plans/2026-11-25-phase-0-clean-slate.md](docs/plans/2026-11-25-phase-0-clean-slate.md): the binding spec for the current code.
- [docs/design/2026-11-24-revamp-design.md](docs/design/2026-11-24-revamp-design.md): goals, decisions D1 to D17, architecture, security model, roadmap.
- [docs/design/2026-11-24-agent-design.md](docs/design/2026-11-24-agent-design.md): the Phase 1 agent.
- [docs/adr/](docs/adr): decisions worth remembering.
- [docs/configuration.md](docs/configuration.md) and [docs/compile.md](docs/compile.md): settings, and what the compile hardening does.
