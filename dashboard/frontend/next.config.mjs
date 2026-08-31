/**
 * The dashboard frontend never talks to an API cross-origin.
 *
 * `/api/dashboard/*` and `/api/equity-shadow/*` are rewritten to loopback
 * FastAPI processes, so the browser only ever sees one origin. That removes
 * the need for a CORS policy entirely - and a CORS policy is a thing you can
 * get wrong, on APIs that have no authentication in front of them.
 *
 * **Two upstreams, deliberately.** They are separate processes, running as
 * separate users, reading separate databases. `:8000` reads the trading
 * database as the trading service identity; `:8001` reads the shadow's own
 * record as an identity that cannot open the trading database, cannot read
 * the broker credentials, and cannot read the file that authorizes paper
 * submission. One process serving both would have handed the research reader
 * production reach; the split is the point, and this file is where it becomes
 * visible.
 *
 * Both rewrites are still GET-only at the edge: Caddy answers 405 to any
 * method other than GET or HEAD before either upstream is reached.
 *
 * `DASHBOARD_API_ORIGIN` and `EQUITY_SHADOW_API_ORIGIN` exist only to move the
 * ports. Both are read at build and server start, never shipped to the
 * browser, and neither is `NEXT_PUBLIC_`.
 */
const apiOrigin = process.env.DASHBOARD_API_ORIGIN ?? "http://127.0.0.1:8000";
const shadowApiOrigin = process.env.EQUITY_SHADOW_API_ORIGIN ?? "http://127.0.0.1:8001";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/api/dashboard/:path*",
        destination: `${apiOrigin}/api/dashboard/:path*`,
      },
      {
        source: "/api/equity-shadow/:path*",
        destination: `${shadowApiOrigin}/api/equity-shadow/:path*`,
      },
    ];
  },
};

export default nextConfig;
