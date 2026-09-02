/**
 * The dashboard frontend never talks to an API cross-origin.
 *
 * Five API prefixes are rewritten to five loopback FastAPI processes, so the
 * browser only ever sees one origin. That removes the need for a CORS policy
 * entirely - and a CORS policy is a thing you can get wrong, on APIs that have
 * no authentication in front of them.
 *
 * **Five upstreams, deliberately.** They are separate processes, running as
 * separate users, reading separate records - and one of those records is at a
 * different schema version from the others, which is why "separate" here is
 * enforced by the kernel rather than by convention:
 *
 *   /api/dashboard/*          :8000  the trading database and the broker
 *                                    account, as the trading service identity
 *   /api/equity-shadow/*      :8001  the V3 + EDA-1 observation record, as an
 *                                    identity that cannot open the trading
 *                                    database or read a credential
 *   /api/equity-paper/*       :8002  the equity paper record, the deployed
 *                                    policy, the merged order list, and the
 *                                    service manager's view of the units
 *   /api/equity-a1b-shadow/*  :8003  the A1-B U30 observation record, as the
 *                                    one identity that can read it
 *   /api/market-charts/*      :8004  provider bars for the charts and nothing
 *                                    else; opens no record at all
 *
 * All five rewrites are still GET-only at the edge: Caddy answers 405 to any
 * method other than GET or HEAD before any upstream is reached.
 *
 * The `*_API_ORIGIN` variables exist only to move the ports. They are read at
 * build and server start, never shipped to the browser, and none is
 * `NEXT_PUBLIC_`.
 */
const apiOrigin = process.env.DASHBOARD_API_ORIGIN ?? "http://127.0.0.1:8000";
const shadowApiOrigin = process.env.EQUITY_SHADOW_API_ORIGIN ?? "http://127.0.0.1:8001";
const paperApiOrigin = process.env.EQUITY_PAPER_API_ORIGIN ?? "http://127.0.0.1:8002";
const a1bApiOrigin = process.env.EQUITY_A1B_SHADOW_API_ORIGIN ?? "http://127.0.0.1:8003";
const chartsApiOrigin = process.env.MARKET_CHARTS_API_ORIGIN ?? "http://127.0.0.1:8004";

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
      {
        source: "/api/equity-paper/:path*",
        destination: `${paperApiOrigin}/api/equity-paper/:path*`,
      },
      {
        source: "/api/equity-a1b-shadow/:path*",
        destination: `${a1bApiOrigin}/api/equity-a1b-shadow/:path*`,
      },
      {
        source: "/api/market-charts/:path*",
        destination: `${chartsApiOrigin}/api/market-charts/:path*`,
      },
    ];
  },
};

export default nextConfig;
