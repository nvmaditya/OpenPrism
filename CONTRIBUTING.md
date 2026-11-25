# Contributing to PrismTeX

Thank you for helping. This file covers how to set up, test and submit a change. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- For a bug, open an issue with the bug form. For a feature or a larger change, open an issue first so we can agree on the approach before you write code.
- Report security problems privately, as [SECURITY.md](SECURITY.md) describes, not in an issue.
- The plan is in [ROADMAP.md](ROADMAP.md), the design in [docs/design](docs/design) and the decisions in [docs/adr](docs/adr). [CONTEXT.md](CONTEXT.md) is the glossary: please use its terms in code, docs and UI text.

## Setup

You need Python 3.12 or newer, Node.js 22, and TeX Live or MiKTeX with `latexmk` (MiKTeX also needs Perl).

```bash
python3 -m venv .venv                                # a venv: not optional on every system, see below
.venv/bin/python -m pip install -e 'backend[dev]'    # the prismtex package, with pytest, ruff and pyright
cd frontend && npm ci && cd ..
./start.sh                                           # API on 127.0.0.1:8000, UI on 127.0.0.1:3000
```

The virtual environment is required on systems whose Python is externally managed (PEP 668): on Ubuntu 24.04, Debian and Homebrew Python a bare `pip install` fails with `externally-managed-environment`. On Windows Git Bash the interpreter is `.venv/Scripts/python`; on Debian and Ubuntu, `python3 -m venv` may first need `sudo apt install python3-venv`. `start.sh` picks up `.venv` in the repository root automatically.

## Tests and checks

Run these before you open a pull request. CI runs the same lanes, and the `ci-ok` check must pass before a merge.

Backend, from `backend/`:

```bash
ruff check src tests
ruff format --check src tests
pyright
pytest -m "not tex"     # fast lane: no TeX needed
pytest -m tex           # compile lane: needs latexmk, and fails (never skips) without it
```

Frontend, from `frontend/`:

```bash
npm run lint
npm run typecheck
npm run build
```

Tests build their own settings over a temporary data directory, so they never read your `.env` or touch `./data`. A test that needs a real TeX installation carries the `tex` marker.

## Rules the code keeps

- **Agent changes are reviewed first.** The agent never writes a project folder. Its edits reach a project only as a proposal the user accepts or rejects hunk by hunk ([ADR 0003](docs/adr/0003-review-first-proposals.md)). A change that lets AI-written text reach a paper unseen will not be merged.
- **Project folders hold only the user's files.** Settings, logs, build output and history live under the data directory, outside the project ([ADR 0004](docs/adr/0004-project-folders-hold-only-user-files.md)).
- **The server builds every compile command.** Nothing from a request or a project becomes a command-line argument except a validated root file and one of the three engines. Shell escape stays off. See [docs/compile.md](docs/compile.md).
- **No new runtime dependencies without discussion.** Open an issue first; development-only tools are easier to agree on.
- **The UI calls the API only through its own origin** (`/api/*`), and the API keeps its Host and Origin checks ([ADR 0005](docs/adr/0005-same-origin-api-no-login-on-localhost.md)).

## Commits and pull requests

- Write commit subjects as [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `type(scope): summary`, with types such as `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `ci` and `chore`. Mark a breaking change with `!` (`feat(api)!: ...`). The changelog is generated from these subjects.
- Pull requests are squash-merged, and the PR title becomes the commit subject, so give the PR a Conventional Commit title.
- Keep a pull request to one change, with tests for new behaviour and docs updated in the same PR. Docs describe what exists, not what is planned.
- Text files use LF line endings and UTF-8; `.gitattributes` and `.editorconfig` set this up.
- Dependabot opens weekly update PRs. They are signals: a maintainer applies upgrades in a normal, tested PR and closes the bot's PR.

## AI-assisted contributions

You may use any tools you like, AI assistants included. Whatever tools you use, you are responsible for what you submit: you must understand it, be able to explain it, have tested it, and have the right to contribute it.

## Writing style

Docs, UI text and error messages use plain language: short sentences, concrete words, no marketing. An error message says what happened and what the user can do about it.

## License

PrismTeX is licensed under the [Apache License 2.0](LICENSE). Unless you say otherwise, what you contribute is licensed under the same terms (section 5 of the license).
