import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("the task page says when its stream is down and catches up when it returns", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await mockApi(page, []);
  let down = true;
  // Registered last, so it takes precedence over the stream in mockApi.
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    down
      ? route.abort("connectionreset")
      : route.fulfill({
          contentType: "text/event-stream",
          body: ": keep-alive\n\n",
        }),
  );
  let taskLoads = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/tasks/t1") taskLoads++;
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const notice = page.getByText("Live updates disconnected — reconnecting…");
  await expect(notice).toBeVisible({ timeout: 10_000 });
  const loadsWhileDown = taskLoads;
  down = false;
  await expect(notice).toBeHidden({ timeout: 20_000 });
  await expect.poll(() => taskLoads).toBeGreaterThan(loadsWhileDown);
});

test("a finished task can be deleted after a confirmation, and old screenshots say why they are gone", async ({
  page,
}) => {
  await mockApi(page, []);
  const deleted: string[] = [];
  // Registered last, so these win: a finished task whose screenshot has expired.
  await page.route(detailOf("t1"), async (route) => {
    if (route.request().method() === "DELETE") {
      const headers = await route.request().allHeaders();
      deleted.push(headers["x-csrf-token"]);
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({
      json: { ...task, status: "completed", actions: [] },
    });
  });
  await page.route("**/api/v1/observations/o1/screenshot", (route) =>
    route.fulfill({
      status: 410,
      json: { code: "purged", detail: "deleted under the retention policy" },
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(
    page.getByText("Deleted when its time to be kept ended").first(),
  ).toBeVisible();
  const remove = async () => {
    await page.getByRole("button", { name: "Task actions" }).click();
    await page.getByRole("menuitem", { name: "Delete" }).click();
  };
  await remove();
  const dialog = page.getByRole("dialog", { name: "Delete this task?" });
  await expect(dialog.getByText(/kept, for 90 days/)).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  expect(deleted).toEqual([]);

  await remove();
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect.poll(() => deleted).toEqual(["c1"]);
  await expect(page).toHaveURL(/\/$/);
});

test("a question from the agent is answered in its card", async ({ page }) => {
  await mockApi(page, []);
  const asking = {
    ...task,
    actions: [],
    runs: [{ ...task.runs[0], wait_reason: "user_input" }],
  };
  const sent: unknown[] = [];
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: asking }));
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "agent.question",
        data: { text: "Which size do you want?" },
        created_at: "2026-10-10T10:00:06Z",
      })}\n\n`,
    }),
  );
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: asking });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Which size do you want?")).toBeVisible();
  await expect(
    page.getByText("The agent is waiting for your answer"),
  ).toBeVisible();
  const answer = page.getByPlaceholder("Type your answer");
  await answer.fill("Large");
  await answer.press("Enter");
  await expect.poll(() => sent).toEqual([{ text: "Large" }]);
  // The message box below still takes an answer too.
  await expect(page.getByLabel("Message to the agent")).toHaveAttribute(
    "placeholder",
    "Answer the agent…",
  );
});

test("a question's options are one per line, the recommended one first, each answered with one tap", async ({
  page,
}) => {
  await mockApi(page, []);
  const asking = {
    ...task,
    actions: [],
    runs: [{ ...task.runs[0], wait_reason: "user_input" }],
  };
  const sent: unknown[] = [];
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: asking }));
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "agent.question",
        data: {
          text: "Which Disneyland?",
          options: ["Shanghai Disneyland", "Tokyo Disneyland"],
          recommended: 1,
        },
        created_at: "2026-10-10T10:00:06Z",
      })}\n\n`,
    }),
  );
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: asking });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const group = page.getByRole("group", { name: "Answers the agent suggests" });
  const buttons = group.getByRole("button");
  await expect(buttons).toHaveCount(2);
  await expect(buttons.first()).toContainText("Tokyo Disneyland");
  await expect(buttons.first()).toContainText("(recommended)");
  await buttons.first().click();
  await expect.poll(() => sent).toEqual([{ text: "Tokyo Disneyland" }]);
  // Any other answer can be typed in the card.
  await expect(page.getByPlaceholder("Or type another answer")).toBeVisible();
});

test("a plan the agent presents is approved with a button, or answered from the message box", async ({
  page,
}) => {
  await mockApi(page, []);
  const planning = {
    ...task,
    plan_mode: true,
    actions: [],
    runs: [{ ...task.runs[0], wait_reason: "user_input", wait_detail: "plan" }],
  };
  const sent: unknown[] = [];
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({ json: planning }),
  );
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "plan.proposed",
        data: { text: "1. Open Settings\n2. Turn on dark mode" },
        created_at: "2026-10-10T10:00:06Z",
      })}\n\n`,
    }),
  );
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: planning });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const card = page.getByRole("region", { name: "Plan" });
  await expect(card.getByText("Turn on dark mode")).toBeVisible();
  await expect(
    page.getByText(/Plan mode: the agent looks around and plans/),
  ).toBeVisible();
  await card.getByRole("button", { name: "Keep planning" }).click();
  await expect(page.locator("#note")).toBeFocused();
  await card.getByRole("button", { name: "Approve and run" }).click();
  await expect.poll(() => sent).toEqual([{ text: "/plan approve" }]);
});

const RICH = [
  "Here is the **summary**:",
  "",
  "| Shop | Price |",
  "| --- | --- |",
  "| A | ¥13 |",
  "",
  "```python",
  "def total(prices):",
  "    return sum(prices)  # add them up",
  "```",
  "",
  "```ts",
  "const n: number = 42;",
  "```",
  "",
  "Inline math $a^2 + b^2 = c^2$ and a block:",
  "",
  "$$\\int_0^1 x\\,dx = \\frac{1}{2}$$",
  "",
  "![tracker](https://example.com/pixel.png) <b>raw</b> [source](https://example.com)",
].join("\n");

test("the agent's text shows tables, highlighted code and math, but no outside images or HTML", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "agent.message",
        data: { text: RICH },
        created_at: "2026-10-10T10:00:06Z",
      })}\n\n`,
    }),
  );
  const outside: string[] = [];
  page.on("request", (r) => {
    if (r.url().startsWith("https://example.com")) outside.push(r.url());
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const text = page.locator(".md").first();
  await expect(text.getByRole("table")).toContainText("¥13");
  await expect(text.getByText("python", { exact: true })).toBeVisible();
  await expect(text.locator(".hljs-keyword").first()).toHaveText("def");
  await expect(text.locator(".katex").first()).toBeVisible();
  await expect(text.locator(".katex-display")).toHaveCount(1);
  await expect(text.locator("img")).toHaveCount(0); // a link, not a fetched image
  await expect(text.getByRole("link", { name: "tracker" })).toBeVisible();
  await expect(text.locator("b")).toHaveCount(0); // raw HTML is not rendered
  expect(outside).toEqual([]);
  await expect(page.locator(".md")).toHaveCount(1); // a resent event is shown once
});

test("the person chooses where to buy from the agent's options", async ({
  page,
}) => {
  await mockApi(page, []);
  const choices: unknown[] = [];
  const option = {
    device_id: "d1",
    summary: "1 bag, 2 kg",
    observation_id: null,
  };
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({
      json: {
        ...task,
        runs: [{ ...task.runs[0], wait_reason: "user_input" }],
        actions: [],
        purchases: [
          {
            id: "p1",
            goal: "Cat food",
            options: [
              {
                ...option,
                payee: "Pet World",
                amount: "39.90",
                currency: "CNY",
              },
              {
                ...option,
                payee: "Corner Shop",
                amount: "42.50",
                currency: "CNY",
              },
            ],
            status: "proposed",
            chosen: null,
            decided_at: null,
            created_at: "2026-10-10T10:00:04Z",
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/purchases/p1/choice", (route) => {
    choices.push(route.request().postDataJSON());
    return route.fulfill({ json: {} });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const card = page.getByRole("region", { name: "Where to buy" });
  await expect(card.getByText("Pet World")).toBeVisible();
  await expect(card.getByText("¥42.50")).toBeVisible();
  await card.getByRole("button", { name: "Buy here" }).nth(1).click();
  await expect.poll(() => choices).toEqual([{ option: 1 }]);
});

test("a result is the agent's own words, as a message, with the screenshots it rests on", async ({
  page,
}) => {
  await mockApi(page, []);
  const shots = ["e1", "e2", "e3", "e4", "e5"];
  const result = {
    summary: "Sound is off.",
    evidence_observation_ids: shots,
    completed_at: "2026-10-10T10:05:00Z",
  };
  const done = {
    ...task,
    status: "completed",
    actions: [],
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null, result }],
    observations: shots.map((id) => ({ ...task.observations[0], id })),
    result,
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: done }));
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Sound is off.")).toBeVisible();
  await expect(page.getByText(/^Completed /)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Result" })).toHaveCount(0);
  const open = page.getByRole("button", { name: "Open screenshot" });
  await expect(open).toHaveCount(3);
  await page.getByRole("button", { name: "+2 more" }).click();
  await expect(open).toHaveCount(5);
});

test("a result the agent offers to keep as a skill has a button that asks for one", async ({
  page,
}) => {
  await mockApi(page, []);
  const result = {
    summary: "The page title is Example Domain.",
    evidence_observation_ids: [],
    completed_at: "2026-10-10T10:05:00Z",
    offer_skill: true,
  };
  const done = {
    ...task,
    status: "completed",
    actions: [],
    runs: [{ ...task.runs[0], status: "completed", wait_reason: null, result }],
    result,
  };
  const sent: unknown[] = [];
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: done }));
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: done });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(
    page.getByText("The page title is Example Domain."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Keep as a skill" }).click();
  await expect.poll(() => sent).toEqual([{ text: "/skill" }]);
});

test("a failed task says why at the end of the conversation", async ({
  page,
}) => {
  await mockApi(page, []);
  const result = {
    summary: "The agent reached the step limit (200 steps) without finishing.",
    evidence_observation_ids: [],
    completed_at: "2026-10-10T10:05:00Z",
    failure: "step_limit",
  };
  const search = {
    ...task.actions[0],
    kind: "find_app",
    input: { kind: "find_app", query: "Notes" },
    target: "the installed apps",
    expected_effect: "a list of matching apps",
    effects: [],
    status: "done",
    effect: "no_effect",
    approval: null,
    policy: { mode: "auto", decision: "allow", reasons: [], risk: null },
    intent: null,
  };
  const failed = {
    ...task,
    status: "failed",
    actions: [search],
    runs: [{ ...task.runs[0], status: "failed", wait_reason: null, result }],
    result,
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: failed }));
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  // A reason the system gave is worded by the page, in the reader's language, as a
  // message in red: no card, no heading.
  const why = page.getByText(/most steps one run may take/);
  await expect(why).toBeVisible();
  await expect(why).toHaveCSS("color", /rgb\(/);
  await expect(
    page.getByRole("heading", { name: "Why it failed" }),
  ).toHaveCount(0);
  await expect(page.getByText(/^Ended /)).toHaveCount(0);
});

test("each finished round keeps its result in the conversation after the task goes on", async ({
  page,
}) => {
  await mockApi(page, []);
  const first = {
    summary: "A coffee is about ¥20.",
    evidence_observation_ids: [],
    completed_at: "2026-10-10T10:02:00Z",
  };
  const going = {
    ...task,
    status: "active",
    actions: [],
    result: null,
    runs: [
      {
        ...task.runs[0],
        status: "completed",
        wait_reason: null,
        result: first,
      },
      {
        ...task.runs[0],
        id: "r2",
        status: "running",
        wait_reason: null,
        created_at: "2026-10-10T10:03:00Z",
      },
    ],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: going }));
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("A coffee is about ¥20.")).toBeVisible();
});

test("the agent's words show as a message in the making while the model produces them", async ({
  page,
}) => {
  await mockApi(page, []);
  const speaking = {
    ...task,
    working: true,
    actions: [],
    observations: [],
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
  };
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({ json: speaking }),
  );
  const frame = (text: string) =>
    `event: agent.saying\ndata: ${JSON.stringify({ run_id: task.runs[0].id, text })}\n\n`;
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: frame("It is") + frame("It is **8:07**. Opening Settings"),
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const making = page.locator("#item-saying");
  await expect(making).toContainText("It is 8:07. Opening Settings");
  await expect(making.locator("strong")).toHaveText("8:07"); // Markdown, like a message
  await expect(page.getByText("Thinking about the next step")).toBeVisible();
});

test("the status line says what the step is at, and a refused completion stays faded", async ({
  page,
}) => {
  await mockApi(page, []);
  const speaking = {
    ...task,
    working: true,
    actions: [],
    observations: [],
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
  };
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({ json: speaking }),
  );
  const frame = (text: string, phase: string) =>
    `event: agent.saying\ndata: ${JSON.stringify({ run_id: task.runs[0].id, text, phase })}\n\n`;
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body:
        frame("Sound is off.", "talking") +
        frame("Sound is off.", "checking") +
        frame("Sound is off.", "rejected"),
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Not done yet; working on")).toBeVisible();
  const making = page.locator("#item-saying");
  await expect(making).toContainText("Sound is off.");
  await expect(making).toHaveAttribute("data-refused", "true");
});
