import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("a message sent while the agent works waits queued, and can be taken back or sent now", async ({
  page,
}) => {
  await mockApi(page, []);
  const queuedTask = {
    ...task,
    actions: [],
    inputs: [
      ...task.inputs,
      {
        seq: 9,
        kind: "note",
        text: "Use the other card",
        created_at: "2026-10-10T10:00:08Z",
        queued: true,
        created_by: "u",
      },
    ],
  };
  const calls: string[] = [];
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: queuedTask }),
  );
  await page.route("**/api/v1/tasks/t1/steer", (route) => {
    calls.push("steer");
    return route.fulfill({ json: queuedTask });
  });
  await page.route("**/api/v1/tasks/t1/inputs/9", (route) => {
    calls.push(route.request().method());
    return route.fulfill({ json: { ...queuedTask, inputs: task.inputs } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const queue = page.getByRole("group", {
    name: "Messages waiting for the agent",
  });
  await expect(queue.getByText("Use the other card")).toBeVisible();
  // Not in the conversation yet: the agent has not read it.
  await expect(
    page
      .getByRole("list", { name: "Conversation" })
      .getByText("Use the other card"),
  ).toHaveCount(0);
  await queue.getByRole("button", { name: "Send now" }).click();
  await expect.poll(() => calls).toEqual(["steer"]);
  await queue.getByRole("button", { name: "Take back to edit" }).click();
  await expect.poll(() => calls).toEqual(["steer", "DELETE"]);
  await expect(page.locator("#note")).toHaveValue("Use the other card");
});

test("the task's model can be switched while it runs", async ({ page }) => {
  await mockApi(page, []);
  const switches: unknown[] = [];
  await page.route("**/api/v1/models", (route) =>
    route.fulfill({
      json: [
        {
          name: "gpt-6-luna",
          provider: "openai",
          default: true,
          allowed: true,
        },
        {
          name: "deepseek-v4.1-flash",
          provider: "deepinfra",
          default: false,
          allowed: true,
        },
      ],
    }),
  );
  await page.route("**/api/v1/tasks/t1/model", async (route) => {
    const headers = await route.request().allHeaders();
    switches.push({
      ...route.request().postDataJSON(),
      csrf: headers["x-csrf-token"],
    });
    return route.fulfill({ json: { ...task, model: "deepseek-v4.1-flash" } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  // The new-task form has a model picker too: act only once the task page shows.
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();

  await page.getByRole("button", { name: "Model: gpt-6-luna" }).click();
  await page.getByRole("option", { name: /deepseek-v4\.1-flash/ }).click();
  await expect
    .poll(() => switches)
    .toEqual([{ model: "deepseek-v4.1-flash", csrf: "c1" }]);
});

test("one button sends a message, pauses a running task and resumes a paused one", async ({
  page,
}) => {
  await mockApi(page, []);
  let status = "active";
  const calls: unknown[] = [];
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: { ...task, status } }),
  );
  for (const what of ["pause", "resume", "inputs"]) {
    await page.route(`**/api/v1/tasks/t1/${what}`, (route) => {
      calls.push(what === "inputs" ? route.request().postDataJSON() : what);
      if (what !== "inputs") status = what === "pause" ? "paused" : "active";
      return route.fulfill({ json: { ...task, status } });
    });
  }
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const box = page.getByLabel("Message to the agent");
  await box.fill("Use the blue one");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => calls).toEqual([{ text: "Use the blue one" }]);

  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect.poll(() => calls.at(-1)).toBe("pause");

  // Writing to a paused task carries on with it, as in a chat.
  await box.fill("Now the red one");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect
    .poll(() => calls.slice(-2))
    .toEqual([{ text: "Now the red one" }, "resume"]);
});

test("the first message offers /plan, and a task is created with it", async ({
  page,
}) => {
  await mockApi(page, []);
  const created: unknown[] = [];
  await page.route("**/api/v1/tasks", (route, request) => {
    if (request.method() !== "POST") return route.fallback();
    created.push(request.postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  const box = page.locator("#goal");
  await box.fill("/pl");
  await expect(
    page.getByText(/Plan first: the agent looks around/),
  ).toBeVisible();
  await box.press("Tab");
  await expect(box).toHaveValue("/plan ");
  await box.pressSequentially("Turn on dark mode");
  await box.press("Enter");
  await expect
    .poll(() => (created[0] as { goal: string } | undefined)?.goal)
    .toBe("/plan Turn on dark mode");
});

test("a new task asks which group to go in, and is filed there from the start", async ({
  page,
}) => {
  await mockApi(page, []);
  const created: unknown[] = [];
  await page.route("**/api/v1/task-groups", (route) =>
    route.fulfill({
      json: [{ id: "g1", name: "Errands", created_at: "2026-10-10T10:00:00Z" }],
    }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/tasks",
    (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      created.push(route.request().postDataJSON());
      return route.fulfill({ status: 201, json: task });
    },
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  await page.getByRole("button", { name: "Group: No group" }).click();
  await page.getByRole("option", { name: "Errands" }).click();
  await page.getByLabel("Goal").fill("Open Settings");
  await page.keyboard.press("Enter");
  await expect
    .poll(() => created)
    .toEqual([
      expect.objectContaining({ goal: "Open Settings", group_id: "g1" }),
    ]);
});

test("a running task can be told to think harder, in its model's menu", async ({
  page,
}) => {
  await mockApi(page, []);
  const changes: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/reasoning-effort", (route) => {
    changes.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...task, reasoning_effort: "high" } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  // The new-task form has a model menu too: act only once the task page shows.
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
  await page.getByRole("button", { name: "Model: gpt-6-luna, Medium" }).click();
  await page.getByRole("radio", { name: "High", exact: true }).click();
  await expect.poll(() => changes).toEqual([{ reasoning_effort: "high" }]);
});

test("a running task takes another phone from its message box but keeps the one it has", async ({
  page,
}) => {
  await mockApi(page, []);
  const pixel = {
    id: "d1",
    name: "Pixel 8",
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: "t1",
    task_goal: "Buy the item in the cart",
    pool: "Desk Mac",
    problems: [],
    enabled: true,
  };
  const galaxy = {
    ...pixel,
    id: "d2",
    name: "Galaxy S30",
    task_id: null,
    task_goal: null,
  };
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({ json: [pixel, galaxy] }),
  );
  const changes: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/devices", (route) => {
    changes.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...task, device_ids: ["d1", "d2"] } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await page.getByRole("button", { name: /^Phones: Pixel 8/ }).click();
  await expect(page.getByRole("option", { name: /Pixel 8/ })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  await page.getByRole("option", { name: /Galaxy S30/ }).click();
  await expect.poll(() => changes).toEqual([{ device_ids: ["d1", "d2"] }]);
});

test("typing / offers /goal, and a /goal message is sent as it is", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const box = page.locator("#note");
  await box.fill("/g");
  await expect(page.getByText("Set a new goal for this task")).toBeVisible();
  await box.press("Tab");
  await expect(box).toHaveValue("/goal ");
  await box.pressSequentially("Check the battery");
  await expect(
    page.getByText(/Sending makes this the task's goal/),
  ).toBeVisible();
  await box.press("Enter");
  await expect.poll(() => sent).toEqual([{ text: "/goal Check the battery" }]);
});

test("the first message offers /goal, and a task is created with it", async ({
  page,
}) => {
  await mockApi(page, []);
  const created: unknown[] = [];
  await page.route("**/api/v1/tasks", (route, request) => {
    if (request.method() !== "POST") return route.fallback();
    created.push(request.postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  const box = page.locator("#goal");
  await box.fill("/g");
  await expect(page.getByText(/Start with a goal/)).toBeVisible();
  await box.press("Tab");
  await expect(box).toHaveValue("/goal ");
  await box.pressSequentially("Check the battery");
  await box.press("Enter");
  await expect
    .poll(() => (created[0] as { goal: string } | undefined)?.goal)
    .toBe("/goal Check the battery");
});

test("a message of the person's can be copied, edited into the message box, or sent again", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await mockApi(page, []);
  const sent: unknown[] = [];
  const running = {
    ...task,
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
    actions: [],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: running }),
  );
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: running });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await page.getByRole("button", { name: "Copy", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toBe(task.inputs[0].text);
  await page.getByRole("button", { name: "Edit and send again" }).click();
  const box = page.getByLabel("Message to the agent");
  await expect(box).toHaveValue(task.inputs[0].text);
  await expect(box).toBeFocused();
  await page.getByRole("button", { name: "Send again", exact: true }).click();
  await expect.poll(() => sent).toEqual([{ text: task.inputs[0].text }]);
});

test("a model whose provider the workspace does not allow cannot be chosen", async ({
  page,
}) => {
  await mockApi(page, []);
  const switches: unknown[] = [];
  await page.route("**/api/v1/models", (route) =>
    route.fulfill({
      json: [
        {
          name: "gpt-6-luna",
          provider: "openai",
          default: true,
          allowed: true,
        },
        {
          name: "deepseek-v4.1-flash",
          provider: "deepinfra",
          default: false,
          allowed: false,
        },
      ],
    }),
  );
  await page.route("**/api/v1/tasks/t1/model", (route) => {
    switches.push(route.request().postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();

  await page.getByRole("button", { name: "Model: gpt-6-luna" }).click();
  const blocked = page.getByRole("option", { name: /deepseek-v4\.1-flash/ });
  await expect(blocked).toHaveAttribute("aria-disabled", "true");
  await expect(blocked).toContainText("deepinfra is not allowed in Settings");
  await blocked.click({ force: true });
  expect(switches).toEqual([]);
});

test("choosing a model keeps its menu open, to set the effort right after", async ({
  page,
}) => {
  await mockApi(page, []);
  const switches: unknown[] = [];
  await page.route("**/api/v1/models", (route) =>
    route.fulfill({
      json: [
        {
          name: "gpt-6-luna",
          provider: "openai",
          default: true,
          allowed: true,
        },
        {
          name: "deepseek-v4.1-flash",
          provider: "deepinfra",
          default: false,
          allowed: true,
        },
      ],
    }),
  );
  await page.route("**/api/v1/tasks/t1/model", (route) => {
    switches.push(route.request().postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();

  await page.getByRole("button", { name: "Model: gpt-6-luna" }).click();
  await page.getByRole("option", { name: /deepseek-v4\.1-flash/ }).click();
  await expect.poll(() => switches).toEqual([{ model: "deepseek-v4.1-flash" }]);
  await expect(
    page.getByRole("radio", { name: "High", exact: true }),
  ).toBeVisible();
});

test("what is written to a task stays after leaving the page and after a reload, until it is sent", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/inputs", (route, request) => {
    if (request.method() !== "POST") return route.fallback();
    sent.push(request.postDataJSON());
    return route.fulfill({ status: 201, json: task });
  });
  await signIn(page);
  await openTask(page);

  const box = page.locator("#note");
  await box.fill("Half a thought");
  await page.goto("/devices");
  await page.goto("/tasks/t1");
  await expect(box).toHaveValue("Half a thought");
  await page.reload();
  await expect(box).toHaveValue("Half a thought");

  await box.press("Enter");
  await expect.poll(() => sent).toEqual([{ text: "Half a thought" }]);
  await expect(box).toHaveValue("");
  await page.reload();
  await expect(box).toHaveValue("");
});

test("a new task's message and constraints stay after a reload", async ({
  page,
}) => {
  await mockApi(page, []);
  await signIn(page);
  await page.goto("/");
  await page.locator("#goal").fill("Turn on dark mode");
  await page.reload();
  await expect(page.locator("#goal")).toHaveValue("Turn on dark mode");
});

test("a finished task's model can still be switched, and a new task starts with the settings last chosen", async ({
  page,
}) => {
  await mockApi(page, []);
  const models = [
    { name: "gpt-6-luna", provider: "openai", default: true, allowed: true },
    {
      name: "deepseek-v4.1-flash",
      provider: "deepinfra",
      default: false,
      allowed: true,
    },
  ];
  await page.route("**/api/v1/models", (route) =>
    route.fulfill({ json: models }),
  );
  const finished = {
    ...task,
    status: "completed",
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null }],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: finished }),
  );
  const switched: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/model", (route) => {
    switched.push(route.request().postDataJSON());
    return route.fulfill({
      json: { ...finished, model: "deepseek-v4.1-flash" },
    });
  });
  const created: { model?: string }[] = [];
  await page.route("**/api/v1/tasks", (route, request) => {
    if (request.method() !== "POST") return route.fallback();
    created.push(request.postDataJSON());
    return route.fulfill({ json: task });
  });
  await signIn(page);
  await openTask(page);

  const picker = page.getByRole("button", { name: /^Model: gpt-6-luna/ });
  await expect(picker).toBeEnabled();
  await picker.click();
  await page.getByRole("option", { name: /deepseek-v4\.1-flash/ }).click();
  await expect.poll(() => switched).toEqual([{ model: "deepseek-v4.1-flash" }]);

  await page.goto("/");
  await expect(
    page.getByRole("button", { name: /^Model: deepseek-v4\.1-flash/ }),
  ).toBeVisible();
  await page.locator("#goal").fill("Open Settings");
  await page.locator("#goal").press("Enter");
  await expect.poll(() => created[0]?.model).toBe("deepseek-v4.1-flash");
});

test("a model that goes down to low offers it, and one that does not starts its task at medium", async ({
  page,
}) => {
  await mockApi(page, []);
  const fromMedium = ["medium", "high", "xhigh", "max"];
  await page.route("**/api/v1/models", (route) =>
    route.fulfill({
      json: [
        {
          name: "gpt-6-luna",
          provider: "openai",
          default: true,
          allowed: true,
          efforts: fromMedium,
        },
        {
          name: "gpt-6.1-sol",
          provider: "openai",
          default: false,
          allowed: true,
          efforts: ["low", ...fromMedium],
        },
      ],
    }),
  );
  const created: { model?: string; reasoning_effort?: string }[] = [];
  await page.route("**/api/v1/tasks", (route, request) => {
    if (request.method() !== "POST") return route.fallback();
    created.push(request.postDataJSON());
    return route.fulfill({ json: task });
  });
  await signIn(page);

  await page.getByRole("button", { name: /^Model: gpt-6-luna/ }).click();
  await expect(
    page.getByRole("radio", { name: "Low", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("option", { name: /gpt-6\.1-sol/ }).click();
  await page.getByRole("radio", { name: "Low", exact: true }).click();
  await page.getByRole("option", { name: /gpt-6-luna/ }).click();
  await expect(
    page.getByRole("radio", { name: "Low", exact: true }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.locator("#goal").fill("Open Settings");
  await page.locator("#goal").press("Enter");
  await expect
    .poll(() => created[0])
    .toMatchObject({ reasoning_effort: "medium" });
});
