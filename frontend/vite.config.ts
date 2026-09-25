import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Built assets are served by FastAPI under /app/ (CSP: no inline scripts/styles).
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: { outDir: "dist", assetsDir: "assets", sourcemap: false, chunkSizeWarningLimit: 900 },
  server: { proxy: { "/api": "http://localhost:8000" } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
