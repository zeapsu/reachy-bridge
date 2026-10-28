import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Where `bun run dev` forwards API calls: a running controller (Rust or the
// old Python one), on its default port unless CONTROLLER_URL says otherwise.
const controller = process.env.CONTROLLER_URL ?? "http://127.0.0.1:7861";
const page = (name: string) => fileURLToPath(new URL(name, import.meta.url));

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rolldownOptions: {
      input: {
        index: page("./index.html"),
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: controller, changeOrigin: true, ws: true },
      // The robot state stream lives at /telemetry/state-stream, outside /api.
      "/telemetry": { target: controller, changeOrigin: true },
    },
  },
});
