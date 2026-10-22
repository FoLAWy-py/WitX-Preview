import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { signIn } from "./fixtures/auth";

/** Counts the requests for each of the workspace's lists. */
function countLists(page: Page) {
  const asked = { tasks: 0, groups: 0, devices: 0, schedules: 0 };
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/tasks") asked.tasks += 1;
    if (path === "/api/v1/task-groups") asked.groups += 1;
    if (path === "/api/v1/devices") asked.devices += 1;
    if (path === "/api/v1/schedules") asked.schedules += 1;
  });
  return asked;
}

const notice = (seq: number, kind: string) =>
  `id: ${seq}\nevent: ${kind}\ndata: ${JSON.stringify({
    seq,
    type: kind,
    data: { task_id: null, type: null },
    created_at: "2026-10-10T10:00:00Z",
  })}\n\n`;

test("a notice that devices changed loads the devices again, and nothing else", async ({
  page,
}) => {
  await mockApi(page, []);
  // Streams opened before the notice is sent get it (the page may have opened one it
  // closed again: React runs effects twice in development); later ones get nothing.
  let send = false;
  let sent = false;
  await page.route("**/api/v1/workspace/events", async (route) => {
    if (sent) return route.fulfill({ status: 503, json: {} });
    await expect.poll(() => send, { timeout: 20_000 }).toBe(true);
    sent = true;
    await route
      .fulfill({
        contentType: "text/event-stream",
        body: ": listening\n\n" + notice(1, "devices"),
      })
      .catch(() => {}); // a stream the page already closed
  });
  await signIn(page);
  await page.waitForURL("/");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.waitForTimeout(300);
  const asked = countLists(page);

  send = true;

  await expect.poll(() => asked.devices).toBe(1);
  await page.waitForTimeout(400);
  expect(asked).toEqual({ tasks: 0, groups: 0, devices: 1, schedules: 0 });
});

test("with no notices the workspace does not poll every few seconds, only once a minute", async ({
  page,
}) => {
  await page.clock.install();
  await mockApi(page, []);
  await signIn(page);
  await page.waitForURL("/");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  const asked = countLists(page);

  await page.clock.fastForward(30_000);
  expect(asked.tasks + asked.groups).toBe(0);

  await page.clock.fastForward(31_000);
  await expect.poll(() => asked.groups).toBeGreaterThan(0);
});
