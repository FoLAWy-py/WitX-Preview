import { defineConfig } from "@playwright/test";
const portText = process.env.WITX_BROWSER_TEST_PORT ?? "5174";
const port = Number(portText);
if (!/^[0-9]{4,5}$/.test(portText) || port < 1024 || port > 65535) {
  throw new Error("Invalid WITX_BROWSER_TEST_PORT");
}
const baseURL = `http://127.0.0.1:${port}`;
export default defineConfig({
  testDir: "./tests/browser",
  use: { baseURL },
  // The owner uses the app in Safari, so every flow runs in WebKit, Safari's engine, too.
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
  webServer: {
    command: `pnpm dev --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
  },
});
