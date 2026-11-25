# PrismTeX

[![CI](https://github.com/nvmaditya/OpenPrism/actions/workflows/ci.yml/badge.svg)](https://github.com/nvmaditya/OpenPrism/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A self-hosted LaTeX editor, being built around an AI agent that shows its work: every edit a reviewable diff that compiles, every citation checked, on the model you choose.

> **Status: pre-release.** Nothing has been released yet. The writing agent is being rebuilt and returns in v0.1.0; until then PrismTeX is a local LaTeX workspace without AI features.

## What works today

- **Projects.** Create a blank project, an example paper or an example résumé; import a ZIP (with limits on size, file count and compression ratio, and symlinks and paths outside the project rejected); rename and delete.
- **Files.** A file tree with upload into any folder, rename and delete. Saves carry the version the editor loaded, so a file changed on disk since you opened it is never silently overwritten.
- **Editor.** A plain text editor for now. CodeMirror arrives in Phase 2.
- **Outline.** The headings of every `.tex` file, each a link to its line.
- **Compile.** latexmk with pdflatex, xelatex or lualatex, so bibliographies and cross-references resolve. Shell escape is off, TeX cannot open files outside the project by path, and build output stays outside the project folder. See [docs/compile.md](docs/compile.md).
- **Diagnostics.** Errors and warnings with file and line; clicking one opens the file at that line.
- **PDF preview.** The compiled PDF in the browser's own PDF viewer. A pdf.js viewer with SyncTeX arrives in Phase 2.
- **Project settings.** The root file and the TeX engine are set per project.

## Quickstart

You need:

- TeX Live or MiKTeX with `latexmk` (MiKTeX also needs Perl);
- Python 3.12 or newer;
- Node.js 22.

Install the backend, then start the API and the UI together:

```bash
git clone https://github.com/nvmaditya/OpenPrism.git
cd OpenPrism
pip install -e 'backend[dev]'
./start.sh
```

Open <http://127.0.0.1:3000>. `start.sh` uses `.venv` if there is one, installs the UI's packages on first run, keeps data in `./data` and writes logs to `./logs`. Ctrl+C stops both servers. On Windows, run it from Git Bash.

To run the two servers yourself, in two terminals:

```bash
python -m prismtex serve        # API on http://127.0.0.1:8000
```

```bash
cd frontend && npm install && npm run dev    # UI on http://127.0.0.1:3000
```

A Docker image arrives in Phase 4.

## Configuration

Settings come from `PRISMTEX_*` environment variables or a `.env` file in the directory the server starts from; copy `.env.example` to begin. [docs/configuration.md](docs/configuration.md) lists them all. The ones you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `PRISMTEX_DATA_DIR` | `data` | Where projects and their state live |
| `PRISMTEX_HOST`, `PRISMTEX_PORT` | `127.0.0.1`, `8000` | Where the API listens |
| `PRISMTEX_ACCESS_TOKEN` | unset | Required to bind beyond loopback |
| `PRISMTEX_COMPILE_TIMEOUT_S` | `120` | Seconds before a compile is stopped |

The server binds 127.0.0.1 and refuses any other address unless an access token is set. Read [SECURITY.md](SECURITY.md) before exposing it to a network.

## Architecture

```mermaid
flowchart LR
  B[Browser] -->|same origin| W[Next.js UI<br/>/api/* proxied]
  W --> A[FastAPI on 127.0.0.1]
  A --> P[(Project folders<br/>user files only)]
  A --> S[(Project state<br/>settings, compile logs)]
  A --> C[Compiler<br/>latexmk]
  A -.Phase 1.-> M[Writing agent<br/>your choice of model]
```

The backend is the `prismtex` Python package in `backend/src/prismtex`, and the UI is a Next.js app in `frontend/`. Project folders hold only your files: settings and compile logs live under `<data dir>/state/`, and build output under `<data dir>/build/`. The design documents in [docs/design](docs/design) and the decision records in [docs/adr](docs/adr) explain the rest.

## Roadmap

[ROADMAP.md](ROADMAP.md) has the phases and their exit criteria. In short: Phase 1 brings the new agent and v0.1.0, Phase 2 the real editor and PDF viewer, Phase 3 citation checks, and Phase 4 self-hosting with Docker.

## Contributing

Contributions are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests and commit style, and everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md). For questions, see [SUPPORT.md](SUPPORT.md).

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md). Please do not open a public issue.

## License

[Apache License 2.0](LICENSE). See also [NOTICE](NOTICE).

PrismTeX is an independent project. It is not affiliated with or endorsed by OpenAI.
