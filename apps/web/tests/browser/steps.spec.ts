import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";

const step = (id: string, target: string, at: string, done: string | null) => ({
  ...task.actions[0],
  id,
  target,
  effects: [],
  status: done ? "done" : "sent",
  effect: done ? "unverified" : "not_attempted",
  approval: null,
  intent: null,
  post_observation_id: null,
  policy: {
    mode: "auto",
    decision: "allow",
    reasons: [],
    risk: null,
  },
  created_at: at,
  finished_at: done,
});

test("a finished round is one line with no count or time; its steps show only when unfolded", async ({
  page,
}) => {
  await mockApi(page, []);
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [
      step("a1", "Settings", "2026-10-10T10:00:05Z", "2026-10-10T10:00:07Z"),
      step("a2", "Sounds", "2026-10-10T10:01:00Z", "2026-10-10T10:02:10Z"),
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const round = page.getByRole("button", { name: "Worked on the phone" });
  await expect(round).toBeVisible();
  await expect(page.getByText(/steps|2m 05s/)).toHaveCount(0);
  await expect(page.getByText("Sounds")).toHaveCount(0);
  await round.click();
  await expect(page.getByText("Policy")).toHaveCount(0);
  await page.getByRole("button", { name: /Sounds/ }).click();
  await expect(page.getByText("Policy")).toBeVisible();
});

test("a step's screenshot shows only when the step is unfolded; opening or searching an app has none", async ({
  page,
}) => {
  await mockApi(page, []);
  const shot = (id: string) => ({
    ...task.observations[0],
    id,
    captured_at: "2026-10-10T10:01:30Z",
  });
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [
      {
        ...step("a1", "", "2026-10-10T10:00:05Z", "2026-10-10T10:00:07Z"),
        kind: "find_app",
        input: { kind: "find_app", query: "王者荣耀" },
        effect: "no_effect",
        post_observation_id: "o3",
      },
      {
        ...step("a2", "", "2026-10-10T10:00:20Z", "2026-10-10T10:00:25Z"),
        kind: "launch_app",
        input: { kind: "launch_app", app_id: "com.tencent.smoba" },
        target: "Honor of Kings",
        post_observation_id: "o4",
      },
      {
        ...step("a3", "Start", "2026-10-10T10:01:00Z", "2026-10-10T10:01:30Z"),
        post_observation_id: "o5",
      },
    ],
    observations: [...task.observations, shot("o3"), shot("o4"), shot("o5")],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await signIn(page);
  await openTask(page);

  await page.getByRole("button", { name: "Worked on the phone" }).click();
  const after = page.getByRole("button", { name: "Screenshot after" });
  await expect(after).toHaveCount(0); // folded into each step
  for (const name of [/王者荣耀/, /Honor of Kings/])
    await page.getByRole("button", { name }).click();
  await expect(after).toHaveCount(0);
  await page.getByRole("button", { name: /Start/ }).click();
  const tap = page.getByRole("listitem").filter({ hasText: "Start" });
  await expect(
    tap.getByRole("button", { name: "Screenshot after" }),
  ).toBeVisible();
  await expect(page.getByText("Policy")).toHaveCount(3);
});

test("a step under way or waiting for you says so nowhere in its line; one that ended otherwise says how", async ({
  page,
}) => {
  await mockApi(page, []);
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [
      {
        ...step("a1", "Settings", "2026-10-10T10:00:05Z", null),
        status: "sent",
      },
      {
        ...step("a2", "Sounds", "2026-10-10T10:01:00Z", "2026-10-10T10:01:02Z"),
        status: "rejected",
      },
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await signIn(page);
  await openTask(page);

  await page.getByRole("button", { name: "Worked on the phone" }).click();
  await expect(page.getByText("Sending")).toHaveCount(0);
  await expect(page.getByText("· Not sent")).toBeVisible();
});

test("a single step is folded like any other; unfolded, its effect says what it changes", async ({
  page,
}) => {
  await mockApi(page, []);
  const plant = {
    ...step("a1", "Plant", "2026-10-10T10:00:05Z", "2026-10-10T10:00:07Z"),
    effects: ["change_data"],
    changes: "plant cherries",
    expected_effect: "Cherries are planted on plot 3 for 874 coins",
  };
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [plant],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await signIn(page);
  await openTask(page);

  await page.getByRole("button", { name: "Worked on the phone" }).click();
  const chip = page.getByText("Changes: plant cherries");
  await expect(chip).toBeVisible();
  await expect(chip.locator("..")).toHaveAttribute(
    "title",
    "Cherries are planted on plot 3 for 874 coins",
  );
});

test("an effect the agent did not describe is named by its kind", async ({
  page,
}) => {
  await mockApi(page, []);
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [
      step("a1", "Settings", "2026-10-10T10:00:05Z", "2026-10-10T10:00:07Z"),
      {
        ...step("a2", "Save", "2026-10-10T10:01:00Z", "2026-10-10T10:01:10Z"),
        effects: ["change_data"],
      },
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await signIn(page);
  await openTask(page);

  await page.getByRole("button", { name: "Worked on the phone" }).click();
  await expect(page.getByText("Changes data")).toBeVisible();
});

test("while steps run, the round's line says what the agent does now, with no count", async ({
  page,
}) => {
  await mockApi(page, []);
  const running = {
    ...task,
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
    actions: [
      step("a1", "Settings", "2026-10-10T10:00:05Z", "2026-10-10T10:00:07Z"),
      step("a2", "Sounds", "2026-10-10T10:01:00Z", null),
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: running }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const doing = page.getByText("Tap “Sounds” on Pixel 8");
  await expect(doing).toHaveCount(1);
  await expect(page.getByText(/Worked on the phone|steps/)).toHaveCount(0);
  // The status line is the round's line, and the last thing in the conversation.
  const last = page.locator("ol > li").last();
  await expect(last).toContainText("Tap “Sounds” on Pixel 8");
  await last.getByRole("button").first().click();
  await expect(page.getByText("Settings")).toBeVisible();
});

test("a long press, a double tap and a held swipe are named in the steps and marked on the screen", async ({
  page,
}) => {
  await mockApi(page, []);
  const gesture = (
    id: string,
    input: Record<string, unknown>,
    at: string,
    after: string | null = null,
  ) => ({
    ...step(id, "Joystick", at, at),
    kind: input.kind,
    input,
    post_observation_id: after,
  });
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [
      gesture(
        "a1",
        { kind: "long_press", x: 300, y: 900, duration_ms: 800 },
        "2026-10-10T10:00:05Z",
      ),
      gesture(
        "a2",
        { kind: "double_tap", x: 300, y: 900 },
        "2026-10-10T10:00:10Z",
      ),
      gesture(
        "a3",
        {
          kind: "swipe",
          ...{ x1: 200, y1: 2000, x2: 120, y2: 2000 },
          ...{ duration_ms: 150, hold_ms: 3000 },
        },
        "2026-10-10T10:00:20Z",
        "o1",
      ),
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await page.getByRole("button", { name: "Worked on the phone" }).click();
  await expect(page.getByText("Long press", { exact: true })).toBeVisible();
  await expect(page.getByText("Double tap", { exact: true })).toBeVisible();
  // The last screen shows the held swipe: an arrow, and a dashed ring where it held.
  const shown = page.getByRole("img", { name: /with the input marked/ });
  await expect(shown).toBeVisible();
  await expect(shown.locator("line")).toHaveCount(2);
  await expect(shown.locator("circle[stroke-dasharray]")).toHaveCount(1);
});

test("what the agent did along the way is a line of its round, and the round's live line while it thinks after it", async ({
  page,
}) => {
  await mockApi(page, []);
  const running = {
    ...task,
    working: true,
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
    actions: [
      step("a1", "Settings", "2026-10-10T10:00:05Z", "2026-10-10T10:00:07Z"),
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: running }),
  );
  const event = (seq: number, type: string, data: object) =>
    `data: ${JSON.stringify({ seq, type, data, created_at: "2026-10-10T10:00:09Z" })}\n\n`;
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body:
        event(1, "agent.web_search_on", { reason: "news" }) +
        event(2, "agent.web_search", { query: "pixel 8 sounds" }),
    }),
  );
  await signIn(page);
  await openTask(page);

  // Not a divider of its own: the live line says it while the agent thinks after it.
  const last = page.locator("ol > li").last();
  await expect(last).toContainText("Searched the web: “pixel 8 sounds”");
  await expect(page.getByText(/Turned on web search/)).toHaveCount(0);
  await last.getByRole("button").first().click();
  await expect(page.getByText("Turned on web search: news")).toBeVisible();
  await expect(page.getByText("Settings")).toBeVisible();
});

test("a round's screenshots open as one gallery that fits the window and moves with arrows, keys and thumbnails", async ({
  page,
}) => {
  await mockApi(page, []);
  const shot = (id: string, at: string) => ({
    ...task.observations[0],
    id,
    captured_at: at,
  });
  const finished = {
    ...task,
    status: "stopped",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
    actions: [
      {
        ...step(
          "a1",
          "Settings",
          "2026-10-10T10:00:05Z",
          "2026-10-10T10:00:07Z",
        ),
        post_observation_id: "o4",
      },
      {
        ...step("a2", "Sounds", "2026-10-10T10:01:00Z", "2026-10-10T10:01:02Z"),
        post_observation_id: "o5",
      },
    ],
    observations: [
      ...task.observations,
      shot("o4", "2026-10-10T10:00:08Z"),
      shot("o5", "2026-10-10T10:01:03Z"),
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  await signIn(page);
  await openTask(page);

  await page.getByRole("button", { name: "Worked on the phone" }).click();
  await page.getByRole("button", { name: /Sounds/ }).click();
  await page.getByRole("button", { name: "Screenshot after" }).click();
  const gallery = page.getByRole("dialog");
  await expect(gallery.getByText("2 / 2")).toBeVisible(); // opened on the one clicked
  // The phone, before the row of thumbnails.
  const phone = gallery.getByRole("img", { name: "Screenshot" }).first();
  const box = (await phone.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize()!.height);

  await page.keyboard.press("ArrowLeft");
  await expect(gallery.getByText("1 / 2")).toBeVisible();
  await gallery.getByRole("button", { name: "Next screenshot" }).click();
  await expect(gallery.getByText("2 / 2")).toBeVisible();
  await gallery.getByRole("button", { name: "Screenshot 1" }).click();
  await expect(gallery.getByText("1 / 2")).toBeVisible();
});
