import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle (server.js + traced node_modules) keeps the Docker runtime image small.
  output: "standalone",
  // The console is served and documented at http://127.0.0.1:3000. Since Next 16 the dev server
  // answers 403 to cross-site-looking requests (JS chunks, HMR socket) from any host not listed
  // here, and `localhost` is the only default, so a page opened via 127.0.0.1 never hydrates.
  // Dev-only: has no effect on `next build` / `next start` / the standalone image.
  allowedDevOrigins: ["127.0.0.1"],
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
