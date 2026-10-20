import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// In development the API runs on :8000 (`make dev-api`); requests are proxied to it.
const api = process.env.WITX_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // `ws`: the takeover socket (/api/v1/takeovers/{id}/connect) goes through too.
    proxy: { "/api": { target: api, ws: true } },
  },
  // The Markdown, math and code-highlighting chunk (~600 kB) loads only with agent text.
  build: { chunkSizeWarningLimit: 650 },
});
