import { defineConfig } from "@playwright/test";
const portText = process.env.WITX_BROWSER_TEST_PORT ?? "47174";
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
  // The built app, as deployed: bundled pages load at once, where the dev server compiled
  // every module on first request, most of a run's time in CI.
  webServer: {
    command: `pnpm build && pnpm preview --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
  },
});
