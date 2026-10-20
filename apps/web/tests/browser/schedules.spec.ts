import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { scheduleView } from "./fixtures/schedule";

test("a task can be set to run every week instead of now", async ({ page }) => {
  await mockApi(page, []);
  const made: Record<string, unknown>[] = [];
  const listed: unknown[] = [];
  await page.route("**/api/v1/schedules", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      made.push(body);
      const view = {
        ...body,
        id: "s1",
        enabled: true,
        next_run_at: "2026-10-14T01:00:00Z",
        last_run_at: null,
        last_task_id: null,
        last_outcome: null,
      };
      listed.push(view);
      return route.fulfill({ status: 201, json: view });
    }
    return route.fulfill({ json: listed });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  await page.locator("#goal").fill("Check the battery");
  await page.getByRole("button", { name: "More options" }).click();
  await page.getByRole("menuitem", { name: "Run on a schedule…" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Run this task on a schedule",
  });
  await dialog.getByRole("button", { name: /^Repeat:/ }).click();
  await page.getByRole("option", { name: "Every week" }).click();
  await dialog.getByRole("button", { name: "Wed" }).click();
  await dialog.getByLabel("Time").fill("09:00");
  await dialog.getByRole("button", { name: "Set schedule" }).click();
  await page.getByRole("button", { name: "Schedule task" }).click();

  await expect.poll(() => made.length).toBe(1);
  expect(made[0]).toMatchObject({
    goal: "Check the battery",
    repeat: "weekly",
    weekdays: [0, 2], // Monday is chosen to start with
    time_of_day: "09:00",
  });
  await expect(page.getByText(/^Scheduled\. It first runs/)).toBeVisible();
});

test("a schedule the agent proposes runs only after it is turned on, in the task's time zone", async ({
  page,
}) => {
  await mockApi(page, []);
  const proposed = {
    id: "s1",
    goal: "Check the battery",
    constraints: [],
    device_ids: ["d1"],
    permission_mode: "auto",
    model: "gpt-6-luna",
    reasoning_effort: "medium",
    repeat: "daily",
    time_of_day: "08:00:00",
    timezone: "Asia/Shanghai",
    weekdays: [],
    month_day: null,
    run_on: null,
    enabled: false,
    next_run_at: null,
    last_run_at: null,
    last_task_id: null,
    last_outcome: "proposed",
  };
  const changes: unknown[] = [];
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({ json: [proposed] }),
  );
  await page.route("**/api/v1/schedules/s1", (route) => {
    changes.push(route.request().postDataJSON());
    return route.fulfill({
      json: { ...proposed, enabled: true, next_run_at: "2026-10-14T00:00:00Z" },
    });
  });
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "schedule.proposed",
        data: {
          schedule_id: "s1",
          goal: "Check the battery",
          repeat: "daily",
          time_of_day: "08:00",
          weekdays: [],
          month_day: null,
          run_on: null,
        },
        created_at: "2026-10-10T10:00:06Z",
      })}\n\n`,
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Proposed schedule")).toBeVisible();
  await expect(page.getByText("Every day at 08:00").first()).toBeVisible();
  await expect(page.getByText(/Asia\/Shanghai/)).toBeVisible();
  await page.getByRole("button", { name: "Turn on" }).click();
  await expect.poll(() => changes).toEqual([{ enabled: true }]);
});

test("on the schedules page, a schedule's menu opens the task it last started; one that never started does not offer it", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({
      json: [
        scheduleView("s1", "Open Notes", {
          last_task_id: "t1",
          last_outcome: "started",
          last_run_at: "2026-10-16T00:00:00Z",
        }),
        scheduleView("s2", "Check the battery"),
      ],
    }),
  );
  await signIn(page);
  await page.goto("/schedules");

  await page
    .getByRole("button", { name: "Actions for Check the battery" })
    .click();
  await expect(
    page.getByRole("menuitem", { name: /last started/ }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Actions for Open Notes" }).click();
  await page.getByRole("menuitem", { name: /last started/ }).click();
  await expect(page).toHaveURL(/\/tasks\/t1$/);
});

test("the schedules page lists active ones soonest first, then paused and proposed, then completed; the sidebar counts the active", async ({
  page,
}) => {
  await mockApi(page, []);
  const hour = 3_600_000;
  const active = Array.from({ length: 7 }, (_, i) =>
    scheduleView(`a${i}`, `Active ${i + 1}`, {
      // Listed latest first: the page puts the soonest first.
      next_run_at: new Date(Date.now() + (7 - i) * hour).toISOString(),
    }),
  );
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({
      json: [
        ...active,
        scheduleView("p1", "Paused one", { enabled: false, next_run_at: null }),
        scheduleView("p2", "Proposed one", {
          enabled: false,
          next_run_at: null,
          last_outcome: "proposed",
        }),
        scheduleView("c1", "Ran once", {
          repeat: "once",
          run_on: "2026-10-15",
          next_run_at: null,
          last_run_at: "2026-10-15T00:00:00Z",
          last_task_id: "t1",
          last_outcome: "started",
        }),
      ],
    }),
  );
  await signIn(page);
  await expect(page.getByRole("link", { name: "Scheduled 7" })).toBeVisible();
  await page.getByRole("link", { name: "Scheduled 7" }).click();

  const section = (name: string) => page.getByRole("region", { name });
  const goals = section("Active").getByRole("listitem");
  await expect(goals).toHaveCount(7);
  await expect(goals.first()).toContainText("Active 7");
  await expect(goals.last()).toContainText("Active 1");
  await expect(section("Paused").getByRole("listitem")).toHaveCount(2);
  await expect(
    section("Paused").getByRole("button", { name: "Turn on" }),
  ).toBeVisible(); // the proposed one
  await expect(section("Completed").getByText("Ran once")).toBeVisible();
});

test("a schedule is edited in one place: what it does, on which phones, and when", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: Record<string, unknown>[] = [];
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({ json: [scheduleView("s1", "Check the battery")] }),
  );
  await page.route("**/api/v1/schedules/s1", (route) => {
    sent.push(route.request().postDataJSON() as Record<string, unknown>);
    return route.fulfill({ json: scheduleView("s1", "Check the battery") });
  });
  await signIn(page);
  await page.goto("/schedules");

  await page.getByRole("button", { name: "Edit" }).click();
  const editor = page.getByRole("dialog", { name: "Edit schedule" });
  await editor
    .getByRole("textbox", { name: "What it does" })
    .fill("Check the battery and the storage");
  await editor.getByRole("button", { name: "Every day" }).click();
  await page.getByRole("option", { name: "Every week" }).click();
  await editor.getByRole("button", { name: "Wed" }).click();
  await editor.getByLabel("Time").fill("21:30");
  await editor.getByRole("button", { name: "Save" }).click();

  await expect(editor).toBeHidden();
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({
    goal: "Check the battery and the storage",
    device_ids: ["d1"],
    repeat: "weekly",
    weekdays: [2],
    time_of_day: "21:30",
    timezone: "Asia/Shanghai",
  });
});

test("after a schedule starts, the line under the message box links to its task", async ({
  page,
}) => {
  await mockApi(page, []);
  let due = 0;
  const pending = () =>
    scheduleView("s1", "Open Notes", {
      repeat: "once",
      next_run_at: new Date(due).toISOString(),
    });
  await page.route("**/api/v1/schedules", (route) => {
    if (route.request().method() === "POST") {
      due = Date.now() + 2_000;
      return route.fulfill({ status: 201, json: pending() });
    }
    if (!due) return route.fulfill({ json: [] });
    // What the worker did at the moment: the task started, nothing is due again.
    return route.fulfill({
      json: [
        Date.now() < due
          ? pending()
          : scheduleView("s1", "Open Notes", {
              repeat: "once",
              next_run_at: null,
              last_run_at: new Date(due).toISOString(),
              last_task_id: "t1",
              last_outcome: "started",
            }),
      ],
    });
  });
  await signIn(page);

  await page.locator("#goal").fill("Open Notes");
  await page.getByRole("button", { name: "More options" }).click();
  await page.getByRole("menuitem", { name: "Run on a schedule…" }).click();
  await page
    .getByRole("dialog", { name: "Run this task on a schedule" })
    .getByRole("button", { name: "Set schedule" })
    .click();
  await page.getByRole("button", { name: "Schedule task" }).click();

  await expect(page.getByText(/^Scheduled\. It first runs/)).toBeVisible();
  // Well before the workspace's 30 s refresh.
  const started = page.getByRole("status").filter({ hasText: "Started:" });
  await expect(started).toBeVisible({ timeout: 8_000 });
  await started.getByRole("link", { name: "Open Notes" }).click();
  await expect(page).toHaveURL(/\/tasks\/t1$/);
});
