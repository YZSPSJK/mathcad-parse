import type { NextConfig } from "next";

const config: NextConfig = {
  poweredByHeader: false,
  experimental: { proxyClientMaxBodySize: "52mb" },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${process.env.API_BASE_URL || "http://127.0.0.1:8000"}/api/:path*` }];
  },
};
export default config;
