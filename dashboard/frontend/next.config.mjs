/**
 * The dashboard frontend never talks to the API cross-origin.
 *
 * `/api/dashboard/*` is rewritten to the loopback FastAPI process, so the
 * browser only ever sees one origin. That removes the need for a CORS policy
 * entirely - and a CORS policy is a thing you can get wrong, on an API that
 * has no authentication in front of it.
 *
 * `DASHBOARD_API_ORIGIN` exists only to move the port. It is read at build and
 * server start, never shipped to the browser, and is not `NEXT_PUBLIC_`.
 */
const apiOrigin = process.env.DASHBOARD_API_ORIGIN ?? "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/api/dashboard/:path*",
        destination: `${apiOrigin}/api/dashboard/:path*`,
      },
    ];
  },
};

export default nextConfig;
