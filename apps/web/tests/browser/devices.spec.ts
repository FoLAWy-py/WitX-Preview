import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { SCREEN_JPEG } from "./fixtures/media";
import { task } from "./fixtures/task";

test("the device panel says it is connecting until the first live picture, then live", async ({
  page,
}) => {
  await mockApi(page, []);
  const running = {
    ...task,
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
    actions: [],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: running }));
  let answer: () => void = () => {};
  const ready = new Promise<void>((resolve) => (answer = resolve));
  await page.route("**/api/v1/tasks/t1/devices/d1/live", async (route) => {
    await ready;
    return route.fulfill({
      json: {
        width: 1080,
        height: 2400,
        jpeg: SCREEN_JPEG,
        captured_at: "2026-10-10T10:00:09Z",
      },
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Connecting", { exact: true })).toBeVisible();
  answer();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.getByText("Connecting", { exact: true })).toHaveCount(0);
});

test("a phone that stops answering says why on the task page, which waits for it, and cannot be chosen", async ({
  page,
}) => {
  await mockApi(page, []);
  const away = {
    id: "d1",
    name: "Pixel 8",
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: "t1",
    task_goal: "Buy the item in the cart",
    pool: "Desk Mac",
    problems: ["not_responding"],
    enabled: true,
  };
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({ json: [away] }),
  );
  const waiting = {
    ...task,
    runs: [
      {
        ...task.runs[0],
        wait_reason: "device",
        wait_detail: "waiting for Pixel 8 to reconnect",
      },
    ],
    actions: [],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: waiting }));
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const why =
    "Phone not responding: unlock it, check the connection, or plug it in again";
  await expect(
    page.getByText("Waiting for Pixel 8 to reconnect"),
  ).toBeVisible();
  await expect(page.getByText(why, { exact: true })).toBeVisible();
  await page.goto("/");
  await page.getByRole("button", { name: /^Phones:/ }).click();
  await expect(page.getByRole("option", { name: /Pixel 8/ })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
});

test("a phone that does not respond is not watched: the panel says why and when its screenshot is from, and watching starts when it is back", async ({
  page,
}) => {
  await mockApi(page, []);
  const away = {
    id: "d1",
    name: "Pixel 8",
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: "t1",
    task_goal: "Buy the item in the cart",
    pool: "Desk Mac",
    problems: ["not_responding"],
    enabled: true,
  };
  let back = false;
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({ json: [back ? { ...away, problems: [] } : away] }),
  );
  const waiting = {
    ...task,
    runs: [
      {
        ...task.runs[0],
        wait_reason: "device",
        wait_detail: "waiting for Pixel 8 to reconnect",
      },
    ],
    actions: [],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: waiting }));
  let asked = 0;
  await page.route("**/api/v1/tasks/t1/devices/d1/live", (route) => {
    asked += 1;
    return route.fulfill({
      json: {
        width: 1080,
        height: 2400,
        jpeg: SCREEN_JPEG,
        captured_at: "2026-10-10T10:00:09Z",
      },
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const header = page.locator("header").filter({ hasText: task.request });
  await expect(header.getByText("Waiting for phone")).toBeVisible();
  await expect(header.getByText("Running")).toHaveCount(0);
  await expect(page.getByText("Not responding", { exact: true })).toBeVisible();
  await expect(page.getByText(/^Screenshot from /)).toBeVisible();
  await expect(page.getByText("Connecting", { exact: true })).toHaveCount(0);
  const takeOver = page.getByRole("button", { name: "Take over" });
  await expect(takeOver).toBeDisabled();
  await expect(takeOver).toHaveAccessibleDescription(
    "Phone not responding: unlock it, check the connection, or plug it in again",
  );
  await page.waitForTimeout(2_500);
  expect(asked).toBe(0); // nothing to connect to

  // The phone answers again: the next refresh of the devices starts watching.
  back = true;
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  expect(asked).toBeGreaterThan(0);
  await expect(takeOver).toBeEnabled();
  await expect(header.getByText("Waiting for phone")).toHaveCount(0);
});

test("a locked phone is not watched and says it is locked, but can still be taken over", async ({
  page,
}) => {
  await mockApi(page, []);
  const locked = {
    id: "d1",
    name: "Pixel 8",
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: "t1",
    task_goal: "Buy the item in the cart",
    pool: "Desk Mac",
    problems: ["locked"],
    enabled: true,
  };
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({ json: [locked] }),
  );
  const paused = { ...task, status: "paused", actions: [] };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: paused }));
  let asked = 0;
  await page.route("**/api/v1/tasks/t1/devices/d1/live", (route) => {
    asked += 1;
    return route.abort();
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Locked", { exact: true })).toBeVisible();
  // Taking it over tries waking it; a passcode stays for the person.
  await expect(page.getByRole("button", { name: "Take over" })).toBeEnabled();
  await page.waitForTimeout(1_500);
  expect(asked).toBe(0); // a dark screen has nothing to show
});

test("a live picture that stops coming says so and is asked for less often until one comes", async ({
  page,
}) => {
  await mockApi(page, []);
  const running = {
    ...task,
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
    actions: [],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: running }));
  const asks: number[] = [];
  let answers = false;
  await page.route("**/api/v1/tasks/t1/devices/d1/live", (route) => {
    asks.push(Date.now());
    if (!answers)
      return route.fulfill({ status: 409, json: { code: "device_busy" } });
    return route.fulfill({
      json: {
        width: 1080,
        height: 2400,
        jpeg: SCREEN_JPEG,
        captured_at: "2026-10-10T10:00:09Z",
      },
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  // Three asks in a row without a picture: it says so instead of "connecting".
  await expect(page.getByText("No live picture", { exact: true })).toBeVisible({
    timeout: 6_000,
  });
  await expect(page.getByText("Connecting", { exact: true })).toHaveCount(0);
  // Then it waits twice as long (2 s) before the next ask, and longer after that.
  const asked = asks.length;
  await expect.poll(() => asks.length, { timeout: 4_000 }).toBe(asked + 1);
  answers = true;
  expect(asks[asked - 1] - asks[asked - 2]).toBeLessThan(1_500);
  expect(asks[asked] - asks[asked - 1]).toBeGreaterThan(1_700);
  await expect(page.getByText("Live", { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText("No live picture", { exact: true })).toHaveCount(
    0,
  );
});

test("a phone at work opens the task it works for", async ({ page }) => {
  await mockApi(page, []);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Pixel 8" })
    .click();
  await expect(page).toHaveURL(/\/tasks\/t1$/);
});

test("an owner adds a device pool and sees its one-time code and setup", async ({
  page,
}) => {
  await mockApi(page, []);
  const pool = {
    id: "p1",
    name: "Office Mac",
    host: null,
    state: "waiting",
    connected: false,
    devices: 0,
    last_seen_at: null,
    enroll_expires_at: "2026-10-11T10:00:00Z",
  };
  const pools: (typeof pool)[] = [];
  await page.route("**/api/v1/gateways", (route) => {
    if (route.request().method() === "POST") {
      pools.push(pool);
      return route.fulfill({
        status: 201,
        json: { gateway: pool, code: "tenant-x.secret-code" },
      });
    }
    return route.fulfill({ json: pools });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/devices");

  await page.getByRole("button", { name: "Add device pool" }).click();
  await page.getByLabel("Name").fill("Office Mac");
  await page.getByRole("button", { name: "Create" }).click();
  const dialog = page.getByRole("dialog", { name: "Connect the computer" });
  await expect(dialog.getByText("tenant-x.secret-code")).toBeVisible();
  await expect(
    dialog.getByText("witx-gateway --config gateway.toml enroll"),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByText("Waiting to enroll")).toBeVisible();
});

test("the device pools have a short guide to getting a phone ready with setup", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/gateways", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/devices");

  await page.getByRole("button", { name: "Setup help" }).click();
  const dialog = page.getByRole("dialog", { name: "Get a phone ready" });
  await expect(
    dialog.getByText("witx-gateway --config gateway.toml doctor"),
  ).toBeVisible();
  await expect(
    dialog.getByText("witx-gateway --config gateway.toml setup", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(dialog.getByText("Trust This Computer")).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toBeHidden();
});

test("the device list refreshes in place: the page stays where the reader scrolled", async ({
  page,
}) => {
  await page.clock.install();
  await mockApi(page, []);
  const phones = Array.from({ length: 10 }, (_, i) => ({
    id: `p${i}`,
    name: `Phone ${i}`,
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: null,
    task_goal: null,
    pool: "Desk Mac",
    problems: [],
    enabled: true,
    taken_over: false,
  }));
  let listed = 0;
  await page.route("**/api/v1/devices", (route) => {
    listed += 1;
    return route.fulfill({ json: phones });
  });
  await page.route("**/api/v1/devices/*/changes", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route("**/api/v1/gateways", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/devices#p0"); // as from a phone in the sidebar
  await expect(page.getByRole("heading", { name: "Phone 9" })).toBeAttached();

  const main = page.locator("main");
  await main.evaluate((el) => (el.scrollTop = el.scrollHeight));
  const scrolled = await main.evaluate((el) => el.scrollTop);
  const before = listed;
  await page.clock.runFor(21_000);
  await expect.poll(() => listed).toBeGreaterThan(before);
  expect(await main.evaluate((el) => el.scrollTop)).toBe(scrolled);
});
