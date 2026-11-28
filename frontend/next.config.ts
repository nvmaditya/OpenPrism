import type { NextConfig } from "next";

// The browser only talks to this server's origin; /api is proxied so the
// backend can refuse every cross-origin caller (ADR 0005). Read when the config
// loads: at `next dev` start, and at build time for `next start`.
//
// Token mode (PRISMTEX_ACCESS_TOKEN set on the backend) is API-only in Phase 0:
// the UI has no sign-in and never sends the token, so every proxied request
// would get a 401. Keep this Next server on 127.0.0.1 (the package.json scripts
// bind it there): exposing it on another interface exposes the whole API to that
// network with no token unless the backend itself has a token set. See
// SECURITY.md and docs/configuration.md. (SECURITY-3)
const apiUrl = (process.env.PRISMTEX_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

// Next's rewrite proxy clones every proxied request body with a 10 MB cap by
// default (DEFAULT_BODY_CLONE_SIZE_LIMIT in next/dist/server/body-streams). A
// body over the cap is truncated while its original Content-Length is still
// forwarded, so uvicorn waits for bytes that never arrive and the request hangs
// until `proxyTimeout` instead of returning the backend's 413. Raise the cap
// above the largest backend body limit so uploads (PRISMTEX_MAX_UPLOAD_MB, 50 MB
// default), ZIP imports (PRISMTEX_IMPORT_MAX_MB, 200 MB default) and large text
// saves reach the backend and get a real answer; the backend's own limits still
// enforce the true ceiling. Derived at config load time, so raising
// PRISMTEX_IMPORT_MAX_MB past this value also needs the UI restarted (`next
// dev`) or rebuilt (`next start`). (FRONTEND-1)
const MB = 1024 * 1024;
function clientMaxBodyBytes(): number {
  const uploadMax = Number(process.env.PRISMTEX_MAX_UPLOAD_MB) || 50;
  const importMax = Number(process.env.PRISMTEX_IMPORT_MAX_MB) || 200;
  // Margin covers multipart/form-data framing and JSON escaping of save bodies.
  return (Math.max(uploadMax, importMax) + 56) * MB;
}

const nextConfig: NextConfig = {
  poweredByHeader: false,
  experimental: {
    // The proxy's default 30 s timeout would cut off compiles, which may run
    // for the backend's whole compile timeout (120 s by default).
    proxyTimeout: 10 * 60 * 1000,
    middlewareClientMaxBodySize: clientMaxBodyBytes(),
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default nextConfig;
