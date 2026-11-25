# Configuration

The server reads its settings from environment variables with the `PRISMTEX_` prefix. It also reads a `.env` file in the directory it starts from, and the process environment overrides `.env`. `.env.example` in the repository root lists every variable with its default. Every variable is optional.

## Server

| Variable | Default | Meaning |
|---|---|---|
| `PRISMTEX_DATA_DIR` | `data` | Where projects, project state, build output and history live. A relative path resolves against the working directory at startup. `start.sh` sets it to `<repo>/data`. |
| `PRISMTEX_HOST` | `127.0.0.1` | The interface the API binds. `prismtex serve --host` overrides it. |
| `PRISMTEX_PORT` | `8000` | The port the API binds. `prismtex serve --port` overrides it. |
| `PRISMTEX_ACCESS_TOKEN` | unset | An access token. When it is set, every request except `GET /api/health` must send `Authorization: Bearer <token>` or the cookie `prismtex_token=<token>`. The server refuses to start on a host that is not loopback (for example `0.0.0.0`) unless a token is set. An empty value counts as unset. To serve a LAN, the server's LAN name or IP must also be in `PRISMTEX_ALLOWED_HOSTS` (and in `PRISMTEX_ALLOWED_ORIGINS` for writes from a browser on another origin), or requests fail with `host_not_allowed`. In Phase 0 only API clients can send the token: the UI binds `127.0.0.1` and has no sign-in yet (see [SECURITY.md](../SECURITY.md)). |
| `PRISMTEX_ALLOWED_HOSTS` | empty | Extra host names accepted in the `Host` header, comma-separated. `localhost`, `127.0.0.1` and `::1` are always accepted; any other host is rejected with `host_not_allowed`. |
| `PRISMTEX_ALLOWED_ORIGINS` | empty | Extra origins (`scheme://host[:port]`) allowed to send state-changing requests, comma-separated. Origins on a loopback host, on any port, are always allowed. |
| `PRISMTEX_COMPILE_TIMEOUT_S` | `120` | Seconds before a compile is stopped and its process tree killed. |
| `PRISMTEX_MAX_UPLOAD_MB` | `50` | The largest file one upload may carry, in MB. |
| `PRISMTEX_IMPORT_MAX_MB` | `200` | The largest total unpacked size of an imported ZIP, in MB. |
| `PRISMTEX_IMPORT_MAX_FILES` | `5000` | The most files an imported ZIP may contain. |

Comma-separated values ignore blanks and surrounding spaces, and compare case-insensitively.

## UI and `start.sh`

These are read from the environment only, not from `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `PRISMTEX_API_URL` | `http://127.0.0.1:8000` | Where the Next.js server proxies `/api/*`; the browser only calls the UI's own origin. `npm run dev` reads it at startup; for `npm start` it is fixed when `npm run build` runs. |
| `UI_PORT` | `3000` | The UI port `start.sh` uses. `start.sh` also reads `PRISMTEX_PORT` and `PRISMTEX_DATA_DIR`. |

Keep the Next.js server on `127.0.0.1` (the `package.json` scripts and `start.sh` bind it there). It proxies `/api/*` to the backend with no token of its own, so running it on another interface exposes the whole API to that network — with no token unless the backend itself has `PRISMTEX_ACCESS_TOKEN` set. In Phase 0 the UI cannot send a token anyway, so serve a LAN by putting the backend behind a token and reaching it with an API client, not the UI. See [SECURITY.md](../SECURITY.md).

The UI's `/api/*` proxy caps the size of a request body it will forward, so that large uploads, ZIP imports and text saves reach the backend and get a real answer instead of hanging. It sizes that cap from `PRISMTEX_MAX_UPLOAD_MB` and `PRISMTEX_IMPORT_MAX_MB` when the Next.js config loads — at `npm run dev` start, and at build time for `npm start`. The cap is therefore fixed once the UI is running: if you raise `PRISMTEX_IMPORT_MAX_MB` (or `PRISMTEX_MAX_UPLOAD_MB`) on the backend, restart `npm run dev`, or rebuild with `npm run build` for `npm start`, so the UI picks up the new size. Otherwise a body larger than the old UI cap stalls in the proxy until its timeout instead of returning the backend's `413`, and the backend's own limits still set the true ceiling in either case.

## Model providers

There are no provider settings yet. The writing agent is being rebuilt, and its provider variables (Ollama, Anthropic and OpenAI-compatible endpoints) arrive with it in Phase 1.
