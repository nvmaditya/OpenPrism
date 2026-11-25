import type { NextConfig } from "next";

// The browser only talks to this server's origin; /api is proxied so the
// backend can refuse every cross-origin caller (ADR 0005). Read when the config
// loads: at `next dev` start, and at build time for `next start`.
const apiUrl = (process.env.PRISMTEX_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  poweredByHeader: false,
  experimental: {
    // The proxy's default 30 s timeout would cut off compiles, which may run
    // for the backend's whole compile timeout (120 s by default).
    proxyTimeout: 10 * 60 * 1000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default nextConfig;
