/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    // Proxies /api/* server-side to apps/api, so the browser only ever talks
    // to apps/web's own origin. Without this, the session cookie set by
    // apps/api is a third-party cookie from the browser's perspective (the
    // two apps are served from different domains/subdomains), which modern
    // browsers block or refuse to send back — breaking login silently.
    const apiUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
};

export default nextConfig;
