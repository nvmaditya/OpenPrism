# Phase 0 "Clean slate": implementation spec

- **Date:** 2026-11-25
- **Implements:** revamp design §9 Phase 0 (`docs/design/2026-11-24-revamp-design.md`) and the "Starting point (Phase 0)" line of `docs/design/2026-11-24-agent-design.md`, decisions D5, D7, D11, D12, D13, D16 and §6.3, §6.6, §6.7, §8.
- **Outcome:** safe to run, honest, licensed, renamed, CI green; the old agent retired so the Phase 1 agent is built on a clean base. No release is cut at the end of Phase 0.

## 0. Maintainer decisions taken for this phase

| Question | Decision |
|---|---|
| Q1 name | **PrismTeX**. Python package and slug `prismtex`; environment prefix `PRISMTEX_`; console script `prismtex`; npm package `prismtex-ui`. The GitHub repository stays at `nvmaditya/OpenPrism` until the maintainer renames it on GitHub (GitHub redirects old links). |
| Q2 license | **Apache-2.0**, recommended to the maintainer and open to change in review. |
| Q8 example project | A research-paper template (`paper`) is the default example; the résumé stays as a second example (`resume`). |
| Q9 `AGENTS.md` | A lean, contributor-facing `AGENTS.md`. The in-app agent's rules move into the Phase 1 system prompt. |
| Q10 AI-assisted contributions | Neutral policy in `CONTRIBUTING.md`: contributors are responsible for what they submit, whatever tools they use. |

Read `openprism` in the design documents as `prismtex` and `OPENPRISM_` as `PRISMTEX_`.

## 1. What is removed

- The LangChain/LangGraph agent: `backend/app/agent/*`, `POST /api/projects/{id}/chat`, the "Ask anything" UI, `langchain-*`, `langgraph`.
- The skills subsystem (D11): `backend/app/skills/*`, `/api/skills*`, `data/skill-home/`.
- PNG page previews (D11): `backend/app/previews/*`, `/previews/*`, `pypdf`, `Pillow`, `data/previews/`.
- `POST /api/projects/{id}/tools/run` and `GET /api/projects/tools` (D11: code execution by design).
- Personal tooling and stale planning: `.grok/`, `docs/SHIPLOG.md`, `PLAN.md` (replaced by `ROADMAP.md`), create-next-app boilerplate (`frontend/public/*.svg`, `frontend/README.md`, the default favicon).
- CORS `*` with credentials.
- Writing app state into project folders (`.prism/`), and build artifacts in project folders.

## 2. Backend layout

```
backend/
  pyproject.toml                 # already written: name prismtex, src layout, Apache-2.0, ruff + pyright config, `tex` marker
  src/prismtex/
    __init__.py                  # __version__ = "0.1.0.dev0" (exists)
    __main__.py                  # `python -m prismtex` -> cli.main()
    cli.py                       # `prismtex serve`
    settings.py                  # Settings
    layout.py                    # DataLayout
    errors.py                    # AppError hierarchy, error envelope helpers
    security.py                  # Host / access-token / Origin ASGI middleware
    migrate.py                   # one-time `.prism/` migration
    main.py                      # create_app(settings=None, *, compiler=None) composition root (no module-level app)
    workspace/
      __init__.py                # re-exports the public names below
      paths.py                   # path policy
      text.py                    # content versions, line endings
      store.py                   # ProjectStore
      zipimport.py               # ZIP limits, top-folder strip, detect_root_file
      templates/{blank,paper,resume}/   # package data copied on create
    outline/__init__.py          # ported outline parser
    compile/
      __init__.py                # re-exports
      argv.py                    # build_latexmk_argv, build_tex_env (pure)
      runner.py                  # run_hardened: no shell, stdin closed, timeout + process-tree kill, output cap
      logparse.py                # parse_log (pure)
      compiler.py                # Compiler, CompileResult, Diagnostic
      logs.py                    # compile log store under state/<id>/logs
    api/
      __init__.py
      deps.py                    # FastAPI dependencies: store, compiler, settings from app.state
      health.py
      projects.py
      files.py
      outline.py
      compile.py
  tests/                         # pytest; `-m tex` lane needs real TeX
```

Async tests, if any, use `@pytest.mark.anyio`. Tests build `Settings(_env_file=None, data_dir=tmp_path, ...)` and never read a developer's `.env`. `TestClient` must use `base_url="http://127.0.0.1:8000"` (the Host check rejects `testserver`).

## 3. Settings (`prismtex.settings`)

`Settings(BaseSettings)` with `env_prefix="PRISMTEX_"`, `env_file=".env"` (current working directory), `extra="ignore"`. Nothing else reads `os.environ` for configuration.

| Field | Env | Default | Notes |
|---|---|---|---|
| `data_dir: Path` | `PRISMTEX_DATA_DIR` | `Path("data")` | relative paths resolve against the working directory at startup |
| `host: str` | `PRISMTEX_HOST` | `"127.0.0.1"` | |
| `port: int` | `PRISMTEX_PORT` | `8000` | |
| `access_token: SecretStr \| None` | `PRISMTEX_ACCESS_TOKEN` | `None` | required when `host` is not loopback |
| `allowed_hosts: tuple[str, ...]` | `PRISMTEX_ALLOWED_HOSTS` | `()` | comma-separated extra hostnames for the Host check |
| `allowed_origins: tuple[str, ...]` | `PRISMTEX_ALLOWED_ORIGINS` | `()` | comma-separated extra `scheme://host[:port]` origins |
| `compile_timeout_s: float` | `PRISMTEX_COMPILE_TIMEOUT_S` | `120.0` | |
| `max_upload_mb: int` | `PRISMTEX_MAX_UPLOAD_MB` | `50` | per uploaded file |
| `import_max_mb: int` | `PRISMTEX_IMPORT_MAX_MB` | `200` | total uncompressed ZIP size |
| `import_max_files: int` | `PRISMTEX_IMPORT_MAX_FILES` | `5000` | |

Comma-separated tuples: use pydantic-settings `NoDecode` plus a `field_validator(mode="before")` that splits on commas and strips blanks. `is_loopback_host(host: str) -> bool` lives here: true for `localhost`, any `127.0.0.0/8` address and `::1`; false for `0.0.0.0`, `::` and everything else.

Provider keys are **not** Phase 0 settings; the Phase 1 agent adds them.

## 4. Data layout (`prismtex.layout`)

```python
class DataLayout:
    def __init__(self, data_dir: Path) -> None: ...   # stores data_dir.resolve() as .root
    root: Path
    projects: Path                       # root/projects
    state_root: Path                     # root/state
    build_root: Path                     # root/build
    history_root: Path                   # root/history
    settings_json: Path                  # root/settings.json   (Phase 2)
    ledger: Path                         # root/state/ledger.jsonl (Phase 1)
    def project_dir(self, project_id: str) -> Path   # projects/<id>
    def state(self, project_id: str) -> Path         # state/<id>
    def project_json(self, project_id: str) -> Path  # state/<id>/project.json
    def logs(self, project_id: str) -> Path          # state/<id>/logs
    def build(self, project_id: str) -> Path         # build/<id>
    def out(self, project_id: str) -> Path           # build/<id>/out   (latexmk -outdir)
    def history_git(self, project_id: str) -> Path   # history/<id>.git (Phase 1)
    def ensure(self) -> None                         # mkdir projects, state, build, history
```

Project ids are 32 lowercase hex characters (`uuid4().hex`); `DataLayout` methods do not validate ids, `ProjectStore` does.

## 5. Errors and the envelope (`prismtex.errors`)

Every error response, including middleware rejections, validation errors, unknown routes and 500s, has the body `{"detail": {"code": "<code>", "message": "<plain language>", ...extra}}`.

```python
class AppError(Exception):
    status: int = 400
    code: str = "bad_request"
    def __init__(self, message: str, **extra: object) -> None: ...   # .message, .extra
    def to_body(self) -> dict: ...                                   # {"detail": {"code", "message", **extra}}
```

| Class | `code` | status | extra |
|---|---|---|---|
| `ProjectNotFound` | `project_not_found` | 404 | `project_id` |
| `FileNotFound` | `file_not_found` | 404 | `path` |
| `PathPolicyError` | `path_denied` | 400 | `path`, `reason` |
| `VersionConflict` | `version_conflict` | 409 | `path`, `current_version` (`None` if the file is gone) |
| `FileExists` | `file_exists` | 409 | `path` |
| `NotText` | `not_text` | 415 | `path` |
| `InvalidRequest` | `invalid_request` | 422 | `field` |
| `RootFileMissing` | `root_file_missing` | 422 | `field="root_file"` |
| `RootFileInvalid` | `root_file_invalid` | 422 | `field="root_file"`, `reason` |
| `ImportRejected` | `import_rejected` | 400 | `reason`: `bad_zip`, `zip_slip`, `symlink`, `too_large`, `too_many_files`, `ratio`, `empty` |
| `PayloadTooLarge` | `payload_too_large` | 413 | `limit_mb` |
| `LogNotFound` | `log_not_found` | 404 | `log_id` |
| `PdfNotFound` | `pdf_not_found` | 404 | |

`install_error_handlers(app)` registers handlers for `AppError`, `RequestValidationError` (422 `invalid_request`, `field` = dotted location without the leading `body`/`query`/`path`), Starlette `HTTPException` (`not_found` 404, `method_not_allowed` 405, otherwise `http_error`) and `Exception` (500 `internal_error`, message "Something went wrong on the server.", details only in the server log).

## 6. Security middleware (`prismtex.security`, ADR 0005)

One pure ASGI middleware (no `BaseHTTPMiddleware`, so streaming is unaffected), applied to every HTTP request, in this order:

1. **Host.** Hostname from the `Host` header (strip the port; handle `[::1]:8000`), lowercased. Allowed: `localhost`, `127.0.0.1`, `::1`, plus `settings.allowed_hosts`. Otherwise 400 `host_not_allowed`. A missing `Host` is rejected.
2. **Access token.** Only when `settings.access_token` is set. Every request except `GET /api/health` needs `Authorization: Bearer <token>` or the cookie `prismtex_token=<token>`, compared with `hmac.compare_digest`. Otherwise 401 `unauthorized` with `WWW-Authenticate: Bearer`.
3. **Origin.** For methods other than GET, HEAD and OPTIONS: if `Origin` is present it must be an origin whose hostname is a loopback name (any port), or one listed in `settings.allowed_origins`; `Origin: null` is rejected. If `Origin` is absent and `Sec-Fetch-Site` is `cross-site`, reject. Otherwise allow (non-browser clients). Rejections are 403 `origin_not_allowed`.

No CORS middleware. FastAPI docs move to `/api/docs` and `/api/openapi.json` so they are reachable through the UI proxy.

## 7. CLI (`prismtex.cli`)

`prismtex serve [--host HOST] [--port PORT]` (also `python -m prismtex serve`):

- builds `Settings()`, applying `--host` and `--port`;
- if the host is not loopback and no access token is set, prints `Refusing to bind <host> without PRISMTEX_ACCESS_TOKEN. Set a token, or bind 127.0.0.1.` to stderr and exits with status 2;
- otherwise runs `uvicorn.run(create_app(settings), host=..., port=..., workers=1, proxy_headers=False, server_header=False)`.

`main(argv: list[str] | None = None) -> int`. Tests monkeypatch `uvicorn.run`.

## 8. Workspace (`prismtex.workspace`)

### 8.1 Path policy (`paths.py`)

```python
class PathPolicyError(AppError): ...                 # code path_denied
def check_relative_path(path: str) -> str: ...       # normalized POSIX relative path, or raises
def resolve_in(root: Path, rel: str) -> Path: ...    # check_relative_path + resolve + containment (symlink escape)
```

`check_relative_path` converts `\` to `/`, drops `./` and empty segments, then rejects (each with a `reason`): empty; absolute POSIX (`/x`); Windows drive (`C:\x`, `C:/x`) and drive-relative (`C:x`); UNC (`\\server\share`, `//server`); any `..` segment; NUL or any C0 control character; any of `<>:"|?*` (`:` also covers alternate data streams like `main.tex:ads`); Windows device names `CON PRN AUX NUL COM0-9 LPT0-9` (case-insensitive, with or without any extension, e.g. `con.tex`, `Com1.txt`); segments ending in `.` or space; segments longer than 255 characters; whole paths longer than 240 characters.

`ProjectStore` additionally refuses (for every file-API operation) paths whose first segment is `.git` or `.prism`, case-insensitive, with reason `internal`.

### 8.2 Text and versions (`text.py`)

```python
Eol = Literal["lf", "crlf"]
@dataclass(frozen=True)
class FileVersion:
    text: str          # LF-normalized
    version: str       # content_version of the bytes on disk
    eol: Eol
def content_version(data: bytes) -> str              # SHA-256 hex, 64 digits
def detect_eol(data: bytes) -> Eol                   # "crlf" if CRLF line breaks are the majority, else "lf"
def decode_text(data: bytes) -> tuple[str, Eol]      # UTF-8 (raises UnicodeDecodeError), LF-normalized
def encode_text(text: str, eol: Eol) -> bytes        # normalizes any CRLF in text to LF first, then applies eol
```

A UTF-8 BOM stays part of the text (`\ufeff`) and round-trips. Mixed line endings are normalized to the majority on the next write.

### 8.3 `ProjectStore` (`store.py`)

```python
Engine = Literal["pdflatex", "xelatex", "lualatex"]
ProjectKind = Literal["blank", "paper", "resume", "import"]
SOURCE_EXTENSIONS = frozenset({".tex", ".ltx", ".bib", ".sty", ".cls", ".bst", ".bbx", ".cbx", ".cfg", ".def", ".txt"})

@dataclass(frozen=True)
class ProjectInfo:
    id: str; name: str; kind: ProjectKind; created_at: str; updated_at: str
    root_file: str | None; engine: Engine
    def to_dict(self) -> dict: ...

@dataclass(frozen=True)
class ProjectSettings:
    root_file: str | None; engine: Engine

@dataclass(frozen=True)
class FileEntry:
    path: str; size: int; kind: Literal["source", "asset"]   # kind by extension (SOURCE_EXTENSIONS, case-insensitive)

class ProjectStore:
    def __init__(self, layout: DataLayout, *, max_upload_bytes: int = 50 * 2**20,
                 import_max_bytes: int = 200 * 2**20, import_max_files: int = 5000) -> None
    layout: DataLayout
    # projects
    def create(self, *, kind: Literal["blank", "paper", "resume"] = "blank", name: str | None = None) -> ProjectInfo
    def list(self) -> list[ProjectInfo]                       # newest created first; skips broken project.json
    def get(self, project_id: str) -> ProjectInfo             # ProjectNotFound for bad ids or missing state
    def update(self, project_id: str, *, name: str | None = None, root_file: str | None | Unset = UNSET,
               engine: Engine | None = None) -> ProjectInfo   # one locked read-modify-write of project.json
    def settings(self, project_id: str) -> ProjectSettings
    def delete(self, project_id: str) -> None                 # removes projects/<id>, state/<id>, build/<id>, history/<id>.git
    def project_dir(self, project_id: str) -> Path            # validated; for compile and (Phase 1) History only
    # files
    def list_files(self, project_id: str) -> list[FileEntry]  # sorted by path; excludes .git/ and .prism/
    def tree(self, project_id: str, subpath: str = "") -> dict
    def read(self, project_id: str, path: str) -> FileVersion # FileNotFound, NotText
    def read_bytes(self, project_id: str, path: str) -> tuple[bytes, str]   # (data, version)
    def write(self, project_id: str, path: str, text: str, base_version: str | None) -> FileVersion
    def write_bytes(self, project_id: str, path: str, data: bytes) -> FileEntry
    def upload(self, project_id: str, *, filename: str, data: bytes, folder: str = "") -> FileEntry
    def rename(self, project_id: str, src: str, dst: str) -> None          # FileExists if dst exists
    def delete_file(self, project_id: str, path: str) -> None
    def delete_files(self, project_id: str, paths: list[str]) -> dict      # {"deleted": [...], "errors": [{"path","code","message"}]}
    def import_zip(self, stream: BinaryIO, *, name: str | None = None) -> ProjectInfo
```

- **`project.json`:** `{"schema": 1, "id", "name", "kind", "created_at", "updated_at", "root_file", "engine"}`, written atomically (temp file + `os.replace`). Timestamps are UTC ISO 8601.
- **`create`:** copies `workspace/templates/<kind>/` (via `importlib.resources`) into the new project folder, sets `root_file="main.tex"` and `engine="pdflatex"`. Default names: `Untitled project`, `Example paper`, `Example résumé`. No `prism-uploads/` folder is pre-created.
- **`update(root_file=...)`:** `None` clears it; a string must pass `check_relative_path`, end in `.tex` (case-insensitive), exist as a file, not start with `-`, and contain none of `%#$&~^{}\` or whitespace other than space; otherwise `RootFileInvalid` with a `reason`. `engine` must be one of the three engines (`InvalidRequest`, `field="engine"`). `name` is stripped, 1 to 120 characters.
- **`write`:** the incoming `text` is normalized to LF. If the file exists, its current `eol` is kept; a new file is `lf`. When `base_version` is not `None`: a missing file or a different current version raises `VersionConflict(current_version=...)`. Writes are atomic (temp file in the same directory + `os.replace`). Text over 10 MiB raises `PayloadTooLarge`.
- **`upload`:** `filename` is reduced to its basename (after `\` to `/`), then checked; it lands at `<folder>/<basename>` (`folder` "" = project root) and may overwrite an existing file. Over `max_upload_bytes` raises `PayloadTooLarge`.
- **`tree` node:** `{"name", "path", "type": "dir"}` with `children`, or `{"name", "path", "type": "file", "size", "kind"}`; directories first, then case-insensitive names; `.git` and `.prism` hidden. The root node's `path` is `""` and its name is the project name.
- **Concurrency:** one `threading.RLock` per project id guards `project.json` read-modify-writes and the version check plus write.

### 8.4 ZIP import (`zipimport.py`)

Before extracting anything:

- `BadZipFile`: `ImportRejected(reason="bad_zip")`.
- Any entry that is a symlink (Unix mode `S_IFLNK` in `external_attr >> 16`): reason `symlink`.
- Any entry whose name fails `check_relative_path` (after stripping a trailing `/` for directories): reason `zip_slip`.
- Skipped silently: `__MACOSX/` entries, `.DS_Store`, any path with a `.git` segment, and directory entries.
- Total uncompressed size over the limit: `too_large`. File count over the limit: `too_many_files`. Any entry with `file_size / max(compress_size, 1) > 100` and `file_size > 1 MiB`: `ratio`. No files left: `empty`.
- **Top-folder strip:** if every remaining file shares one first segment, it is removed.
- **`detect_root_file(paths, read_text) -> str | None`:** candidates are `.tex` files whose text contains an uncommented `\documentclass`; prefer `main.tex`, then the fewest path segments, then alphabetical. `None` if none.
- The project is created with `kind="import"`, the detected `root_file` (or `None`), `engine="pdflatex"`, and the name given (or `Imported project`). On any failure after creation, the project is deleted.

### 8.5 Templates (`workspace/templates/`)

- `blank/main.tex`: a minimal article with `\title`, `\author`, `\maketitle` and one section; it compiles cleanly.
- `paper/`: `main.tex` (article; `amsmath`, `amssymb`, `graphicx`, `booktabs`, `hyperref`; title, abstract; `\input` of `sections/introduction.tex`, `sections/method.tex`, `sections/results.tex`, `sections/conclusion.tex`; `\bibliographystyle{plain}` and `\bibliography{references}`), a labelled numbered equation referenced with `\eqref`, a figure that includes `figures/example-figure.png` (a small PNG, at most 4 KB, generated once with pure Python `zlib` and `struct`, and committed), a booktabs table, `\ref` cross-references and `\cite`s of **real** works with correct metadata in `references.bib`: Knuth, *The TeXbook* (Addison-Wesley, 1984); Lamport, *LaTeX: A Document Preparation System*, 2nd ed. (Addison-Wesley, 1994); Mittelbach and Goossens, *The LaTeX Companion*, 2nd ed. (Addison-Wesley, 2004). It must compile under latexmk with no errors and no undefined citations or references.
- `resume/`: the existing modular résumé (`main.tex` plus `sections/header|summary|experience|education|skills.tex`), with "OpenPrism" replaced by "PrismTeX".

All three must compile under `pdflatex` in the TeX lane.

## 9. Migration (`prismtex.migrate`)

`migrate_legacy_projects(layout: DataLayout) -> list[str]` runs once at startup from `create_app`, and is idempotent. For every `projects/<id>/` with `.prism/meta.json`, it:

- writes `state/<id>/project.json` from the old meta if none exists, mapping kind `example` to `resume` (the old example was the résumé) and keeping `blank` and `import`. `root_file` is `main.tex` if that file exists, else the `detect_root_file` result; `engine` is `pdflatex`;
- then removes `.prism/` (old logs are dropped).

Folders without `.prism/` and without `project.json` are left alone. The function returns the migrated ids and logs one line per project.

## 10. Outline (`prismtex.outline`)

Port `parse_tex_outline` unchanged in behaviour. `outline_project(store, project_id) -> list[dict]` reads `.tex` source files through `store.read`: the root file first (when set), then the others in path order, each item `{"title", "line", "level", "macro", "path"}`. Unreadable files are skipped.

## 11. Compile (`prismtex.compile`)

```python
Severity = Literal["error", "warning", "info"]
CompileStatus = Literal["ok", "failed", "timeout", "unavailable"]

@dataclass(frozen=True)
class Diagnostic:
    file: str | None; line: int | None; severity: Severity; kind: str; message: str
    def to_dict(self) -> dict: ...

@dataclass(frozen=True)
class CompileResult:
    status: CompileStatus
    diagnostics: tuple[Diagnostic, ...]
    pdf_path: Path | None         # absolute, inside out_dir; None unless written by this run
    log_text: str                 # latexmk output plus the TeX log, capped at 2 MiB
    duration_ms: int
    engine: Engine
    root_file: str
    exit_code: int | None
    log_id: str | None = None     # set by Compiler.compile when the log is stored
    @property
    def ok(self) -> bool: ...     # status == "ok"

class Compiler:
    def __init__(self, store: ProjectStore, *, timeout_s: float = 120.0) -> None
    def compile(self, project_id: str, *, root_file: str | None = None, engine: Engine | None = None) -> CompileResult
        # defaults from store.settings(); RootFileMissing if unset; root_file validated as in §8.3;
        # one compile per project at a time (a per-project threading.Lock; a second caller waits);
        # out_dir = layout.out(id); stores the log; keeps the newest 20 logs
    def compile_dir(self, src_dir: Path, *, project_id: str, root_file: str, engine: Engine,
                    out_dir: Path, log_path: Path | None = None, timeout_s: float | None = None) -> CompileResult
        # no lock; used by compile() and by Phase 1 scratch compiles
    def logs(self, project_id: str) -> list[dict]        # newest first, metadata only
    def get_log(self, project_id: str, log_id: str) -> dict   # metadata + "log"; LogNotFound
```

### 11.1 argv and environment (`argv.py`, pure, unit-tested)

`build_latexmk_argv(root_file: str, engine: Engine, out_dir: Path) -> list[str]`:

```
latexmk -norc <engine flag> -interaction=nonstopmode -file-line-error -synctex=1 -no-shell-escape -outdir=<out_dir absolute> <root_file>
```

Engine flags: `pdflatex` gives `-pdf`; `xelatex` gives `-pdfxe`; `lualatex` gives `-pdflua -lualatex=lualatex --safer --nosocket %O %S` (one argv element for the `-lualatex=` value). The argv is built only here, from validated inputs; the root file never starts with `-`. There is no `-halt-on-error`, so TeX reports more than one diagnostic.

`build_tex_env(base: Mapping[str, str], out_dir: Path) -> dict[str, str]` keeps only `PATH`, `PATHEXT`, `SYSTEMROOT`/`SystemRoot`, `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `LANG`, `LC_ALL`, `LC_CTYPE`, `MIKTEX_*`, `TEXMFHOME`, `TEXMFVAR`, `TEXMFCONFIG` from `base`, then sets `openin_any=p`, `openout_any=p`, `shell_escape=f`, `TEXMFOUTPUT=<out_dir>`, `max_print_line=10000`, `error_line=254`, `half_error_line=238`. Everything else is dropped, provider keys included: LuaTeX's `os.getenv` can read the environment, as verified on 2026-11-25.

Verified on TeX Live 2023 (2026-11-25): with these settings, `\input{/etc/hostname}`, `\input{../x}`, `\write18` and `\openout` to an absolute path all fail; a project `latexmkrc` is ignored; with `--safer --nosocket`, LuaTeX's `io.open` read and write outside the project fail, and `os.execute` and `io.popen` do nothing; `\cite` and `\ref` resolve through bibtex with `-outdir`.

### 11.2 Runner (`runner.py`)

`run_hardened(argv, *, cwd: Path, env: dict[str, str], timeout_s: float, max_output_bytes: int = 4 * 2**20) -> RunOutcome(exit_code: int | None, output: str, timed_out: bool, duration_ms: int, truncated: bool)`:

- `subprocess.Popen` with `shell=False`, `stdin=DEVNULL`, `stdout=PIPE`, `stderr=STDOUT`;
- POSIX: `start_new_session=True`, and on timeout `os.killpg(pgid, SIGKILL)`; Windows: `CREATE_NEW_PROCESS_GROUP`, and on timeout `taskkill /T /F /PID <pid>`;
- a reader thread keeps at most `max_output_bytes` and drains the rest; output is decoded as UTF-8 with `errors="replace"`;
- the executable comes from `shutil.which(argv[0], path=env["PATH"])`; if it is missing, `Compiler` returns status `unavailable` with one diagnostic, kind `tex_missing`: "latexmk was not found. Install TeX Live or MiKTeX (with Perl) and make sure latexmk is on PATH."

### 11.3 Log parsing (`logparse.py`, pure, table-driven tests with real log fixtures)

`parse_log(text: str, *, src_dir: Path) -> list[Diagnostic]` parses the TeX `.log` file (`<out_dir>/<stem>.log`), and `parse_blg(text) -> list[Diagnostic]` parses the BibTeX `.blg` file. Both are deduplicated and capped at 200.

The parser tracks the current file through the `(` and `)` file stack (a `(` not followed by a path pushes a placeholder, so `)` stays balanced), and maps paths to project-relative POSIX paths: `./sections/a.tex` becomes `sections/a.tex`, and absolute paths inside `src_dir` are made relative. Any other path maps to `None`. Kinds:

| kind | severity | source |
|---|---|---|
| `undefined_control_sequence` | error | `<file>:<line>: Undefined control sequence.` |
| `missing_file` | error | `LaTeX Error: File `x' not found.` |
| `latex_error` | error | other `LaTeX Error:` |
| `package_error` | error | `Package <p> Error:` |
| `tex_error` | error | any other `<file>:<line>: ...` error or `! ...` line |
| `fatal` | error | `! Emergency stop.`, `Fatal error occurred, no output PDF file produced!`, `==> Fatal error occurred`, `job aborted` |
| `undefined_citation` | warning | `Citation `k' on page N undefined on input line L` (LaTeX, natbib, biblatex wording) |
| `undefined_reference` | warning | `Reference `k' on page N undefined on input line L` |
| `multiply_defined_label` | warning | `Label `x' multiply defined` |
| `font_warning` | warning | `LaTeX Font Warning:` |
| `package_warning` | warning | `Package <p> Warning:` (other) |
| `latex_warning` | warning | `LaTeX Warning:` (other) |
| `bad_box` | info | `Overfull`/`Underfull \hbox`/`\vbox` ... `at line(s) N` |
| `bibtex_error` | error | `.blg`: `I couldn't open database file`, `I found no \bibdata command`, other `---` error lines |
| `bibtex_warning` | warning | `.blg`: `Warning--...` |

A warning's line comes from `on input line L` (or `at line(s) N`) and its file from the stack. An error's message is the text after `<file>:<line>: `, with the following `l.<n> ...` context line appended when present.

### 11.4 Status and output

- `timed_out`: status `timeout`, with one diagnostic (kind `timeout`) saying the compile was stopped after N seconds.
- Otherwise `ok` if latexmk exited 0, the PDF was written this run, and there are no error diagnostics; else `failed`.
- `pdf_path` is set when `<out_dir>/<stem>.pdf` exists with an mtime at or after the run's start.
- The project folder gets no build artifacts: latexmk runs with cwd = project folder and `-outdir` in `build/<id>/out`.

### 11.5 Compile logs (`logs.py`)

`state/<id>/logs/<log_id>.log` holds the text; `<log_id>.json` holds `{"log_id", "created_at", "status", "engine", "root_file", "duration_ms", "errors", "warnings"}`. `log_id` is 32 hex characters. Only the newest 20 are kept.

## 12. HTTP API (every route under `/api`)

| Method and path | Request | Response |
|---|---|---|
| `GET /api/health` | | 200 `{"status": "ok", "service": "prismtex", "version"}` (liveness; exempt from the token) |
| `GET /api/projects` | | 200 `{"projects": [ProjectInfo]}` |
| `POST /api/projects` | `{"kind": "blank" \| "paper" \| "resume", "name"?: str}` | 201 `ProjectInfo` |
| `POST /api/projects/import` | multipart `file`, optional `name` | 201 `ProjectInfo`; 400 `import_rejected`; 413 |
| `GET /api/projects/{id}` | | 200 `ProjectInfo`; 404 |
| `PATCH /api/projects/{id}` | `{"name"?: str, "root_file"?: str \| null, "engine"?: Engine}` | 200 `ProjectInfo`; 404; 422 |
| `DELETE /api/projects/{id}` | | 204 |
| `GET /api/projects/{id}/files` | | 200 `{"files": [{"path", "size", "kind"}]}` |
| `GET /api/projects/{id}/tree?path=` | | 200 `{"tree": node}` |
| `GET /api/projects/{id}/files/{path}` | | text: 200 `{"path", "encoding": "utf-8", "content", "version", "eol"}`; binary: 200 `{"path", "encoding": "base64", "content", "version"}`; header `ETag: "<version>"`; 404 |
| `PUT /api/projects/{id}/files/{path}` | `{"content": str}`; optional `If-Match: "<version>"` (quotes optional) | 200 `{"path", "version", "eol"}` + `ETag`; 409 `version_conflict` with `current_version` |
| `POST /api/projects/{id}/upload` | multipart `file`, optional `folder` (relative folder; "" = root) | 201 `{"path", "size", "kind"}`; 413 |
| `POST /api/projects/{id}/files/rename` | `{"from": str, "to": str}` | 200 `{"from", "to"}`; 409 `file_exists` |
| `POST /api/projects/{id}/files/delete` | `{"paths": [str]}` (1 to 1000) | 200 `{"deleted", "errors"}` |
| `DELETE /api/projects/{id}/files/{path}` | | 204 |
| `GET /api/projects/{id}/outline` | | 200 `{"outline": [item]}` |
| `POST /api/projects/{id}/compile` | optional `{"root_file"?: str, "engine"?: Engine}` (this run only, not saved) | 200 `{"status", "ok", "diagnostics": [Diagnostic], "duration_ms", "engine", "root_file", "log_id", "pdf_url"}` where `pdf_url` is `/api/projects/{id}/pdf?v=<pdf mtime_ns>` or null; 404; 422 `root_file_missing` / `root_file_invalid` |
| `GET /api/projects/{id}/pdf` | | 200 `application/pdf` of the root file's PDF in `build/<id>/out/`, `Cache-Control: no-store`, `Content-Disposition: inline; filename="<stem>.pdf"`, `X-Content-Type-Options: nosniff`; 404 `pdf_not_found` |
| `GET /api/projects/{id}/logs` | | 200 `{"logs": [meta]}` newest first |
| `GET /api/projects/{id}/logs/{log_id}` | | 200 meta + `"log"`; 404 `log_not_found` |

`ProjectInfo` JSON: `{"id", "name", "kind", "created_at", "updated_at", "root_file", "engine"}`.

The compile route runs the blocking compile in a worker thread (a plain `def` route or `anyio.to_thread.run_sync`). `create_app(settings=None, *, compiler=None)` lets tests inject a fake compiler with the same `compile`, `logs` and `get_log` methods.

## 13. Frontend (Next.js 15.5.29, React 19, Tailwind 3)

- **`package.json`:** `"name": "prismtex-ui"`, `"version": "0.1.0-dev.0"`, `"license": "Apache-2.0"`, `next` and `eslint-config-next` pinned to `15.5.29`; scripts `dev`, `build`, `start`, `lint` (`eslint .`, flat config), `typecheck` (`tsc --noEmit`).
- **Same origin:** `next.config.ts` rewrites `/api/:path*` to `${process.env.PRISMTEX_API_URL ?? "http://127.0.0.1:8000"}/api/:path*` and sets `poweredByHeader: false`. `lib/api.ts` uses relative `/api/...` URLs only; `NEXT_PUBLIC_API_URL` is gone.
- **`lib/api.ts`:** an `ApiError` class (`status`, `code`, `message`, `detail`) parsed from the envelope. Functions follow §12. `readFile` returns `version` and `eol`; `writeFile(projectId, path, content, baseVersion?)` sends `If-Match` and throws `ApiError` with code `version_conflict` on 409. Removed: `streamChat`, `renderPreviews`, `listSkills`, `health` and every other caller of a removed route.
- **Dashboard:** project list (name and relative created time), search, New menu (Blank, Example paper, Example résumé), Import ZIP, delete. Copy says "PrismTeX".
- **IDE:**
  - file tree with upload into a folder (`folder` form field);
  - textarea editor (CodeMirror arrives in Phase 2);
  - Outline jump.
  - The **Compile** button calls `POST /compile` and shows a status chip (Compiled / Failed / Timed out / TeX not installed) and a **diagnostics list**: severity, `file:line`, message; clicking opens the file at the line.
  - The **Compiled** tab shows the PDF in an `<iframe title="Compiled PDF">` pointed at `pdf_url` (pdf.js arrives in Phase 2).
  - The **Logs** tab lists compile logs, with the raw text behind a disclosure.
  - The **Project Info** tab edits the name, root file (a select of `.tex` files) and engine through `PATCH`.
  - Where the chat used to be, one quiet line: "The writing agent is being rebuilt and returns in v0.1.0."
- **Buffer rule:** no code path overwrites a dirty buffer. Refreshing the tree or outline never reloads the open file. Save sends `If-Match` with the version the buffer was loaded from. On `version_conflict`, a bar says "This file changed on disk since you opened it." with **Reload from disk** (discards the buffer after a `confirm()`) and **Overwrite** (saves with the conflict's `current_version` as the base). Switching files with a dirty buffer asks first.
- **Hygiene:** delete `public/*.svg` boilerplate, `frontend/README.md` and `app/favicon.ico`; add a simple `app/icon.svg`.
- **Gates:** `npm run lint`, `npm run typecheck` and `npm run build` pass with no errors.

## 14. Repository, community and CI

- **New files:**
  - `LICENSE` (the verbatim Apache-2.0 text) and `NOTICE` ("PrismTeX / Copyright 2026 The PrismTeX authors").
  - `README.md` with: the pitch; a status note (pre-release, agent being rebuilt); what works today; a quickstart (native development: TeX Live or MiKTeX, Python 3.12+, Node 22, `./start.sh`; Docker arrives in Phase 4); configuration; an architecture diagram; roadmap; contributing; security; license; and a non-affiliation line ("PrismTeX is an independent project. It is not affiliated with or endorsed by OpenAI.").
  - `CONTRIBUTING.md`: setup, tests and lanes, Conventional Commits, the review-first rule for agent changes, and the neutral policy on AI-assisted contributions.
  - `CODE_OF_CONDUCT.md`: the verbatim Contributor Covenant 3.0.
  - `SECURITY.md`: the threat model (revamp §6.6), the supported deployment shapes (localhost; LAN or Docker only with a token), and private vulnerability reporting through GitHub.
  - `SUPPORT.md`; `CHANGELOG.md` (an "Unreleased" section; generated by git-cliff at release); `cliff.toml`; `ROADMAP.md` (Phases 0 to 4 with exit criteria, Phase 0 marked as in review).
  - `docs/configuration.md` (every `PRISMTEX_*` variable) and `docs/compile.md` (the hardening and what was verified, on which TeX distribution).
  - A lean contributor-facing `AGENTS.md`.
  - `.gitattributes` (LF everywhere, CRLF for `*.cmd` and `*.bat`, binary for images and PDFs) and `.editorconfig`.
- **Changed:** `CONTEXT.md` (product name); the design docs and ADRs get a rename note, Q1 and Q2 recorded as decided, and code identifiers `openprism` and `OPENPRISM_` updated to `prismtex` and `PRISMTEX_` (prose about the old name stays).
- **`.github/`:** `ISSUE_TEMPLATE/bug.yml`, `feature.yml` and `config.yml`; `pull_request_template.md`; `dependabot.yml` (pip `/backend`, npm `/frontend`, github-actions `/`; weekly); `workflows/ci.yml`.
- **`ci.yml`:**
  - Runs on `push` to `main` and `pull_request`, with `permissions: contents: read` and concurrency cancel-in-progress. Actions are pinned by full commit SHA with a `# vX.Y.Z` comment.
  - **`backend`** (matrix `ubuntu-latest`, `windows-latest`; Python 3.12): `pip install -e "backend[dev]"`, `ruff check`, `ruff format --check`, `pyright` (Ubuntu only), `pytest -m "not tex"`.
  - **`tex`** (container `texlive/texlive:latest-small`): `tlmgr install` the packages the templates need (latexmk at least), install Python 3.12 through `astral-sh/setup-uv`, then `pytest -m tex`.
  - **`frontend`** (Node 22): `npm ci`, `lint`, `typecheck`, `build`.
  - **`ci-ok`:** `needs` all jobs, `if: always()`, and fails unless every job succeeded.
- **`start.sh`:** runs `python -m prismtex serve` with `PRISMTEX_DATA_DIR="$ROOT/data"`, waits on `/api/health`, starts the UI with `PRISMTEX_API_URL`, and binds both to 127.0.0.1. `.env.example` lists the Phase 0 variables only.

## 15. Tests that must exist and pass

- **`-m "not tex"`:**
  - settings parsing and `is_loopback_host`; the path-policy table (every rejection reason, and accepted paths with spaces and unicode);
  - `content_version` and EOL round trips: a CRLF file stays CRLF after `write`, a new file is LF, and a stale `base_version` raises `VersionConflict` with the bytes unchanged;
  - store create/list/get/update/delete, including removal of `state/` and `build/`; `.git`/`.prism` refused and hidden; `root_file` validation;
  - ZIP import: slip, symlink, too large, too many files, ratio, top-folder strip, root detection, empty;
  - migration from a legacy `.prism/` folder (idempotent);
  - the API envelope for 404/405/422/500 and each domain error;
  - `ETag`/`If-Match` save, with 409 carrying `current_version`;
  - the Host check (`evil.example` rejected; `localhost:3000` accepted); the Origin check (`https://evil.example` POST rejected, `http://localhost:3000` accepted, a missing Origin accepted, `Sec-Fetch-Site: cross-site` rejected, `Origin: null` rejected); the token (401 without it, 200 with the Bearer header or the cookie, `/api/health` exempt);
  - the CLI refusing `0.0.0.0` without a token (exit 2) and accepting it with one;
  - removed routes return 404 (`/api/projects/{id}/chat`, `/tools/run`, `/api/skills`, `/previews/render`);
  - outline;
  - argv and env builders (no secrets, the flags of §11.1, `--safer --nosocket` for lualatex);
  - log-parser tables over real log fixtures;
  - the runner kills a process tree on timeout (a Python child that spawns a sleeping grandchild) and caps output;
  - compile API routes with an injected fake compiler.
- **`-m tex`** (real TeX; fails, never skips, when latexmk is missing):
  - the paper, résumé and blank templates compile `ok` with no undefined citations or references;
  - the project folder is byte-identical before and after a compile (no artifacts);
  - `\input{/etc/hostname}` (or a Windows equivalent), `\input{../outside}`, `\write18` and an absolute `\openout` are all blocked;
  - a project `latexmkrc` is ignored;
  - lualatex `io.open` outside is blocked, and a secret environment variable is not visible to `os.getenv`;
  - an infinite loop times out and the process tree is gone;
  - an undefined control sequence in `sections/x.tex` yields a diagnostic with that file and line;
  - an undefined citation yields `undefined_citation`;
  - `GET /pdf` serves the result.
