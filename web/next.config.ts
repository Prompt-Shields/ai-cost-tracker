import type { NextConfig } from "next";

// Standalone output keeps the runtime image small (see Dockerfile).
const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
