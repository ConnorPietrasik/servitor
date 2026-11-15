import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built app is served BY the servitor app (same port, same origin):
// base "/" with no proxy — /api/* is same-origin.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    // hashed asset names: safe long-term cache
    assetsDir: "assets",
  },
  server: {
    // dev convenience: proxy the API to a local app instance
    proxy: {
      "/api": "http://127.0.0.1:8471",
      "/panel": "http://127.0.0.1:8471",
      "/healthz": "http://127.0.0.1:8471",
    },
  },
});
