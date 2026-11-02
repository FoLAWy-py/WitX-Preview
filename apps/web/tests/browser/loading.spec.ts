import { expect, test, type Page } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";

// The dev server serves each source file as its own module.
const SETTINGS_CODE = "**/assets/Settings-*.js";
// The Customize page's code, as the build names it.
const customize = (asked: string[]) =>
  asked.some((path) => /\/assets\/Customize-[\w-]+\.js$/.test(path));

async function openSettings(page: Page) {
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
}

function countLoads(page: Page) {
  const loads = { count: 0 };
  page.on("load", () => (loads.count += 1));
  return loads;
}

test("a page whose code is gone after an update opens after one reload", async ({
  page,
  browserName,
}) => {
  // After a real update the reload asks for the new build's file, under another name.
  // Here the name stays, and WebKit keeps a module that failed to load across the reload
  // without asking again, so only the other engine can play this out.
  test.skip(
    browserName === "webkit",
    "the same file name cannot load again in WebKit",
  );
  await mockApi(page, []);
  let refused = false;
  // Gone after an update: the server answers 404 for the old file, once.
  await page.route(SETTINGS_CODE, (route) => {
    if (refused) return route.continue();
    refused = true;
    return route.fulfill({ status: 404 });
  });
  await signIn(page);
  await page.waitForURL("/");
  const loads = countLoads(page);

  await openSettings(page);

  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  expect(page.url()).toMatch(/\/settings$/);
  expect(loads.count).toBe(1);
});

test("a page that still does not open after a reload says so and offers another", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route(SETTINGS_CODE, (route) => route.abort());
  await signIn(page);
  await page.waitForURL("/");
  const loads = countLoads(page);

  await openSettings(page);

  await expect(page.getByText("This page didn't open")).toBeVisible();
  await expect(page.getByRole("button", { name: "Reload" })).toBeVisible();
  expect(loads.count).toBe(1);
});

test("agent text stays readable as plain text when its formatter cannot load", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/assets/Markdown-*.js", (route) => route.abort());
  const result = {
    summary: "**Sound** is off.",
    evidence_observation_ids: [],
    completed_at: "2026-10-10T10:05:00Z",
  };
  const done = {
    ...task,
    status: "completed",
    actions: [],
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null, result }],
    result,
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: done }));
  await signIn(page);
  await openTask(page);

  await expect(page.getByText("**Sound** is off.")).toBeVisible();
});

test("a page that takes a while to open shows a bar at the top until it arrives", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route(SETTINGS_CODE, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });
  await signIn(page);
  await page.waitForURL("/");

  await openSettings(page);

  const bar = page.getByRole("progressbar", { name: "Loading…" });
  await expect(bar).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(bar).toHaveCount(0);
});

test("pointing at a link to a page starts loading it before the click", async ({
  page,
}) => {
  await mockApi(page, []);
  const asked: string[] = [];
  page.on("request", (request) => asked.push(new URL(request.url()).pathname));
  await signIn(page);
  await page.waitForURL("/");
  expect(customize(asked)).toBe(false);

  await page.getByRole("link", { name: "Customize", exact: true }).hover();

  await expect.poll(() => customize(asked)).toBe(true);
  expect(page.url()).not.toMatch(/\/customize$/);
});

test("nothing is loaded ahead while the browser saves data", async ({
  page,
}) => {
  await page.addInitScript(() =>
    Object.defineProperty(navigator, "connection", {
      value: { saveData: true },
    }),
  );
  await mockApi(page, []);
  const asked: string[] = [];
  page.on("request", (request) => asked.push(new URL(request.url()).pathname));
  await signIn(page);
  await page.waitForURL("/");

  await page.getByRole("link", { name: "Customize", exact: true }).hover();

  // A prefetch starts at once (the test above); give it time to show up anyway.
  await page.waitForTimeout(500);
  expect(customize(asked)).toBe(false);
});
