# Configuration

The server reads its settings from environment variables with the `PRISMTEX_` prefix. It also reads a `.env` file in the directory it starts from, and the process environment overrides `.env`. `.env.example` in the repository root lists every variable with its default. Every variable is optional.

## Server

| Variable | Default | Meaning |
|---|---|---|
| `PRISMTEX_DATA_DIR` | `data` | Where projects, project state, build output and history live. A relative path resolves against the working directory at startup. `start.sh` sets it to `<repo>/data`. |
| `PRISMTEX_HOST` | `127.0.0.1` | The interface the API binds. `prismtex serve --host` overrides it. |
| `PRISMTEX_PORT` | `8000` | The port the API binds. `prismtex serve --port` overrides it. |
| `PRISMTEX_ACCESS_TOKEN` | unset | An access token. When it is set, every request except `GET /api/health` must send `Authorization: Bearer <token>` or the cookie `prismtex_token=<token>`. The server refuses to start on a host that is not loopback (for example `0.0.0.0`) unless a token is set. An empty value counts as unset. |
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

## Model providers

There are no provider settings yet. The writing agent is being rebuilt, and its provider variables (Ollama, Anthropic and OpenAI-compatible endpoints) arrive with it in Phase 1.
