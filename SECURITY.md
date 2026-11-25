# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**, or go to <https://github.com/nvmaditya/OpenPrism/security/advisories/new>. Do not open a public issue, pull request or discussion about it.

Include what you found, how to reproduce it, and the impact you expect. We will reply in the advisory, work out a fix with you, agree on when to disclose, and credit you unless you prefer otherwise.

## Supported versions

PrismTeX has not had a release yet. Fixes go to the `main` branch only.

## Supported ways to run it

| Shape | Supported |
|---|---|
| On your own machine, bound to `127.0.0.1` (the default, and what `start.sh` does) | Yes |
| On a LAN or another network interface, with `PRISMTEX_ACCESS_TOKEN` set and the server's LAN name or IP in `PRISMTEX_ALLOWED_HOSTS` (plus `PRISMTEX_ALLOWED_ORIGINS` for writes from a browser on another origin) | Yes, for API clients. In Phase 0 the UI binds `127.0.0.1` and cannot send the token; a browser sign-in arrives in Phase 4. |
| On any non-loopback interface without a token | No. The server refuses to start. |
| Docker | Not yet; an image arrives in Phase 4, under the same token rule |

PrismTeX is a single-user tool. It has no user accounts, and anyone who holds the access token has full access to every project.

The UI's Next.js server must stay on `127.0.0.1` (the `package.json` scripts and `start.sh` bind it there). It proxies `/api/*` to the backend and sends no token of its own, so running it on another interface exposes the whole API to that network — with no token unless the backend itself has `PRISMTEX_ACCESS_TOKEN` set, and even then a browser reaching the UI over the LAN only gets `401`, because the Phase 0 UI has no way to send the token. To serve a LAN, put the backend behind a token and reach it with an API client, not the UI.

## Threat model

What we protect: your unpublished papers, and the other files on your machine.

### A malicious web page calling the local API

A page you visit in your browser could try to send requests to `127.0.0.1:8000`. The controls:

- The UI calls the API through its own origin: the Next.js server proxies `/api/*`.
- The API sends no CORS headers, so other origins cannot read its responses.
- A `Host` allowlist (`localhost`, `127.0.0.1`, `::1` and `PRISMTEX_ALLOWED_HOSTS`) blocks DNS rebinding.
- State-changing requests must come from a loopback origin or one in `PRISMTEX_ALLOWED_ORIGINS`. `Origin: null` is rejected, and so is a cross-site browser request without an `Origin`.
- An access token is required whenever the server binds beyond loopback.

### A malicious project running code or reading files during a compile

An imported ZIP can contain TeX written to attack the machine that compiles it. The hardening below stops a project from choosing its own flags, running shell commands, and writing outside the build folder. It does **not** make reading the filesystem safe: TeX itself has read holes that `openin_any` does not cover, so each is mitigated, not closed.

What the hardening stops:

- The server builds the latexmk command and environment itself, from a validated root file and engine. The root file must be an existing `.tex` file inside the project and must not start with `-`.
- Shell escape is off (`-no-shell-escape`), so `\write18` cannot run commands. latexmk ignores every `latexmkrc`, including one in the project (`-norc`).
- `openin_any=p` and `openout_any=p` make ordinary `\input`, `\openout` and kpathsea reads refuse absolute paths, `..` and dotfiles. Build output goes to the data directory, never into the project folder.
- TeX gets a reduced environment, so the server's secrets are not among the variables it is handed.
- stdin is closed, a timeout kills the whole process tree, and output is capped.
- ZIP imports are checked before anything is extracted: total size, file count, compression ratio, no symlinks, and no paths that leave the project.
- On Windows, the engine and helper tools are named by absolute path, and a project whose root folder holds a file Windows could run as a tool is refused (`409 unsafe_project_file`).

What is only mitigated — the read holes in TeX itself:

- **pdfTeX's `\pdfobj ... file`** copies any file the server account can read into the PDF, bypassing `openin_any`. For `pdflatex` and `xelatex` this is caught by a post-run audit of the `.fls` recorder: if the engine read outside the project, the build folder and the TeX trees, the PDF is deleted and the compile fails with `access_denied`. The audit runs after the fact and depends on the recorder, so it is a mitigation, not a sandbox.
- **LuaLaTeX's Lua layer** reads arbitrary files, directory listings and `/proc/<pid>/environ` even under `--safer`, none of it recorded. So `lualatex` is disabled at compile time this release (a compile with it returns `unavailable`); it is still accepted as a project setting.
- **Biber** can execute code from a crafted control file and read data sources outside the project. It runs through a server-owned guard that vets the `.bcf` and runs `biber --noconf`; where the guard cannot be named (a Python path with a space), Biber is disabled rather than run unguarded.
- **`/proc/<server-pid>/environ`** is closed on Linux by `prctl(PR_SET_DUMPABLE, 0)` at startup — for a same-uid reader only. A server running as **root** is not protected, so do not run PrismTeX as root.

Running a genuinely untrusted project still warrants an OS-level read sandbox or a throwaway container. That is a new system dependency and a tracked follow-up for a later phase, not Phase 0; until it lands, compile untrusted projects under a dedicated low-privilege account with no secrets it can read, or in a container.

[docs/compile.md](docs/compile.md) records what each mitigation does and does not guarantee, what was tested and on which TeX distribution.

### A request reaching files outside the project

Every file path the API accepts is checked against one policy: relative paths only, no `..`, no absolute, drive or UNC paths, no Windows device names or alternate data streams, and no symlink escapes. Project settings and logs live outside the project folder, where the file API cannot reach them.

### Not in this version

There is no AI agent, no model provider and no network lookup in the current code, so no API keys are stored or sent anywhere. These arrive from Phase 1, and this policy will grow with them.

## Out of scope

- Attacks that need a local account on the machine running PrismTeX, or access to its data directory.
- Anything an attacker can do once they hold your access token.
- Bugs in TeX Live, MiKTeX or latexmk themselves. Please report those upstream; tell us if PrismTeX should work around one.
