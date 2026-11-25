# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). At each release, [git-cliff](https://git-cliff.org) generates the entries from the Conventional Commit history, using `cliff.toml`.

## [Unreleased]

Phase 0, "Clean slate": the project is renamed, licensed and made safe to run, and the old agent is retired so the new one can be built on a clean base.

### Added

- Apache License 2.0, with `NOTICE`, a contributor guide, the Contributor Covenant 3.0, a security policy, a support guide, a roadmap and a configuration reference.
- CI on GitHub Actions: backend lint, format, type and test checks on Ubuntu and Windows; a TeX Live lane that compiles real documents; frontend lint, type check and build; and one aggregate `ci-ok` check.
- The `prismtex serve` command (also `python -m prismtex serve`). It refuses to bind beyond loopback without `PRISMTEX_ACCESS_TOKEN`.
- Host and Origin checks on every request, and an optional access token.
- An example research paper (sections, an equation, a figure, a table, cross-references and a bibliography) as the default example; the résumé stays as a second example.
- Compiles through latexmk with pdflatex or xelatex, so bibliographies and cross-references resolve; the root file and engine are set per project. `lualatex` is stored as a project setting but disabled at compile time this release (see Security).
- Structured diagnostics (file, line, severity, kind) in the compile response and a clickable diagnostics list in the UI.
- Saves that carry the version the editor loaded (`ETag` and `If-Match`). The UI shows a conflict bar instead of overwriting a file that changed on disk.
- File rename, single and bulk delete, and upload into any folder.
- A project settings tab for the name, root file and engine.
- One error format for every API error: `{"detail": {"code", "message", ...}}`.
- ZIP import limits (total size, file count, compression ratio, no symlinks), a shared top folder stripped, and the root file detected.

### Changed

- Renamed from OpenPrism to PrismTeX: the Python package is `prismtex` (in `backend/src/prismtex`), settings use the `PRISMTEX_` prefix, and the UI package is `prismtex-ui`.
- The UI calls the API on its own origin through the Next.js proxy. `NEXT_PUBLIC_API_URL` is gone; the UI server reads `PRISMTEX_API_URL`.
- Project state and build output live under the data directory, outside project folders. Projects that still contain `.prism/` are migrated once at startup, and their old logs are dropped.
- Saves keep a file's line endings.
- The compiled PDF opens in the browser's PDF viewer instead of as page images.
- Next.js is updated to 15.5.29.
- The API docs moved to `/api/docs`.

### Removed

- The LangChain and LangGraph agent, the `/chat` route and the "Ask anything" panel. A new agent returns in v0.1.0.
- The skills subsystem and `/api/skills*`.
- PNG page previews and the `/previews` routes, with the `pypdf` and `Pillow` dependencies.
- `POST /api/projects/{id}/tools/run` and `GET /api/projects/tools`.
- Personal tooling, the old plan files and the create-next-app boilerplate.

### Fixed

- Citations and references no longer print as `??`.
- Compile timeouts are enforced on Windows too, and they stop the whole process tree.

### Security

- The API no longer allows every origin with credentials through CORS. It sends no CORS headers and rejects cross-site state-changing requests, so other websites cannot use it.
- Closed the routes to code execution during a compile: the tool runner, a project `latexmkrc`, and argument injection through the root file. Shell escape is off (`-no-shell-escape`, `-norc`), the latexmk command and environment are built only by the server, and the engine and helper tools are named by absolute path.
- Hardened reading during a compile and made the docs honest about its limits. `openin_any=p`/`openout_any=p` stop ordinary `\input` and `\openout` from leaving the project, but TeX has read holes that bypass them, each now mitigated: pdfTeX's `\pdfobj ... file` (caught for pdflatex and xelatex by a post-run `.fls` audit that discards a PDF built from an outside read, with an `access_denied` error), LuaLaTeX's unrecorded Lua I/O (so `lualatex` is refused at compile time this release), and Biber's control-file code execution (routed through a server-owned guard running `biber --noconf`). On Linux `prctl(PR_SET_DUMPABLE, 0)` closes `/proc/<pid>/environ` reads for a same-uid reader; it does not cover a server run as root. On Windows a project file that could be run as a tool is refused (`409 unsafe_project_file`).
- Known limit: the compile hardening is not an OS sandbox. Compiling a genuinely untrusted project still warrants a dedicated low-privilege account or a throwaway container; a real sandbox is a tracked follow-up for a later phase.
- Project settings and logs can no longer be read or written through the file API.
