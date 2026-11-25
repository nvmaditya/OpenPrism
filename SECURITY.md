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
| On a LAN or another network interface, with `PRISMTEX_ACCESS_TOKEN` set | Yes |
| On any non-loopback interface without a token | No. The server refuses to start. |
| Docker | Not yet; an image arrives in Phase 4, under the same token rule |

PrismTeX is a single-user tool. It has no user accounts, and anyone who holds the access token has full access to every project.

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

An imported ZIP can contain TeX written to attack the machine that compiles it. The controls:

- The server builds the latexmk command itself. The root file must be an existing `.tex` file inside the project and must not start with `-`.
- Shell escape is off (`-no-shell-escape`), and latexmk ignores every `latexmkrc`, including one in the project (`-norc`).
- TeX runs with `openin_any=p` and `openout_any=p`, so it cannot reach files outside the project and its build folder through an absolute path or `..`. LuaLaTeX also runs with `--safer --nosocket`.
- TeX gets a reduced environment, so secrets in the server's environment are not visible to it.
- stdin is closed, a timeout kills the whole process tree, and output is capped.
- Build output goes to the data directory, never into the project folder.
- ZIP imports are checked before anything is extracted: total size, file count, compression ratio, no symlinks, and no paths that leave the project.

[docs/compile.md](docs/compile.md) records what was tested and on which TeX distribution.

### A request reaching files outside the project

Every file path the API accepts is checked against one policy: relative paths only, no `..`, no absolute, drive or UNC paths, no Windows device names or alternate data streams, and no symlink escapes. Project settings and logs live outside the project folder, where the file API cannot reach them.

### Not in this version

There is no AI agent, no model provider and no network lookup in the current code, so no API keys are stored or sent anywhere. These arrive from Phase 1, and this policy will grow with them.

## Out of scope

- Attacks that need a local account on the machine running PrismTeX, or access to its data directory.
- Anything an attacker can do once they hold your access token.
- Bugs in TeX Live, MiKTeX or latexmk themselves. Please report those upstream; tell us if PrismTeX should work around one.
