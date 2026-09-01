import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __AGENT2API_DEV_API_TARGET__: JSON.stringify(process.env.VITE_DEV_API_TARGET ?? ""),
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8530",
      "/v1": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8530",
      "/healthz": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8530",
      "/readyz": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:8530",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
