import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle (server.js + traced node_modules) keeps the Docker runtime image small.
  output: "standalone",
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
