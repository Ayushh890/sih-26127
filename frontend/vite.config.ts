/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Development: the Vite dev server proxies the API and WebSocket to the backend, so the
// console is always same-origin (no CORS, no hardcoded API host in the bundle).
const backend = process.env.NIRNAY_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    host: "0.0.0.0",
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/health": backend,
      "/ready": backend,
      "/docs": backend,
      "/openapi.json": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
  preview: { port: 3000, host: "0.0.0.0" },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1200 },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
