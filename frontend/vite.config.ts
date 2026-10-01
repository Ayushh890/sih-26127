/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Development: the Vite dev server proxies the API and WebSocket to the backend, so the
// console is always same-origin (no CORS, no hardcoded API host in the bundle).
// NIRNAY_BACKEND can be set via environment or .env file.

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.NIRNAY_BACKEND ?? process.env.NIRNAY_BACKEND ?? "http://127.0.0.1:8000";
  return {
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    host: "0.0.0.0",
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/health": backend,
      "/ready": backend,
      "/docs": backend,
      "/redoc": backend,
      "/openapi.json": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true, changeOrigin: true },
    },
  },
  preview: { port: 3000, host: "0.0.0.0" },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1200 },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
  };
});
