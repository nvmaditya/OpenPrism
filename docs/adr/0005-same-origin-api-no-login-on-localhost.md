---
status: proposed
---

# Same-origin API and Host checks instead of a login on localhost

The browser talks to the backend through the Next.js server, which proxies `/api/*` on the same origin. The backend:

- binds `127.0.0.1`;
- sends no CORS headers by default;
- rejects requests whose `Host` is not on an allowlist, which blocks DNS rebinding;
- rejects state-changing requests whose `Origin` is not the UI's.

An access token is required only when the server binds beyond loopback (LAN or Docker). It is set through `OPENPRISM_ACCESS_TOKEN`. There is no setting that binds beyond loopback without a token, and the Docker image follows the same rule.

We chose this because the previous setup let any website the user visited call the API (CORS `*` with credentials). Combined with TeX's code-execution features, that was a drive-by attack path. A login screen for a single-user local tool adds friction without adding protection beyond these checks.
