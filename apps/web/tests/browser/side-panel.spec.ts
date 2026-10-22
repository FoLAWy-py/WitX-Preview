import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("the side panel is made wider by dragging its edge, and keeps that width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1400, height: 900 });
  await mockApi(page, []);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const panel = page.getByRole("complementary", { name: "Side panel" });
  const edge = page.getByRole("separator", { name: /Resize the panel/ });
  const before = (await panel.boundingBox())!.width;
  const box = (await edge.boundingBox())!;
  await page.mouse.move(box.x + 2, box.y + 200);
  await page.mouse.down();
  await page.mouse.move(box.x - 118, box.y + 200, { steps: 5 });
  await page.mouse.up();
  await expect
    .poll(async () => Math.round((await panel.boundingBox())!.width))
    .toBe(Math.round(before + 120));

  await page.reload();
  await expect
    .poll(async () => Math.round((await panel.boundingBox())!.width))
    .toBe(Math.round(before + 120));
  await page.getByRole("separator", { name: /Resize the panel/ }).dblclick();
  await expect
    .poll(async () => Math.round((await panel.boundingBox())!.width))
    .toBe(Math.round(before));
});

test("the goal opens in the side panel, where editing it sends a /goal message", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.route("**/api/v1/tasks/t1/side-chat", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await page
    .getByRole("button", { name: /^Goal Buy the item in the cart/ })
    .click();
  await expect(page.getByRole("tab", { name: "Goal" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Goal", { exact: true }).fill("Buy two of them");
  await page.getByRole("button", { name: "Save goal" }).click();
  await expect.poll(() => sent).toEqual([{ text: "/goal Buy two of them" }]);
});

test("a side chat asks about the task and shows the answer when it comes", async ({
  page,
}) => {
  await mockApi(page, []);
  const messages: Record<string, unknown>[] = [];
  await page.route("**/api/v1/tasks/t1/side-chat", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as {
        thread_id: string;
        text: string;
      };
      messages.push({
        id: "m1",
        thread_id: body.thread_id,
        question: body.text,
        answer: null,
        error: null,
        created_at: task.created_at,
        answered_at: null,
      });
      return route.fulfill({ status: 201, json: messages[0] });
    }
    return route.fulfill({ json: messages });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await page.getByRole("button", { name: "Open in the side panel" }).click();
  await page.getByRole("menuitem", { name: "New side chat" }).click();
  await page.getByPlaceholder("Ask about this task…").fill("Why this shop?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByText("Thinking…")).toBeVisible();
  messages[0] = { ...messages[0], answer: "It is the cheapest." };
  await expect(page.getByText("It is the cheapest.")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Why this shop?" })).toBeVisible();
});

test("a task without phones starts with an empty side panel; a closed side chat stays under +", async ({
  page,
}) => {
  await mockApi(page, []);
  const plain = {
    ...task,
    device_ids: [],
    goal: "",
    actions: [],
    observations: [],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: plain }));
  await page.route("**/api/v1/tasks/t1/side-chat", (route) =>
    route.fulfill({
      json: [
        {
          id: "m1",
          thread_id: "c1",
          question: "Why this shop?",
          answer: "It is the cheapest.",
          error: null,
          created_at: task.created_at,
          answered_at: task.created_at,
        },
      ],
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  // No phone, no goal: nothing opens by itself, and no goal line above the box.
  await expect(page.getByRole("tab")).toHaveCount(0);
  await expect(page.getByText(/Nothing open/)).toBeVisible();
  await expect(page.getByRole("button", { name: /^Goal / })).toHaveCount(0);

  await page.getByRole("button", { name: "Open in the side panel" }).click();
  await page.getByRole("menuitem", { name: "Why this shop?" }).click();
  await expect(page.getByText("It is the cheapest.")).toBeVisible();
  await page.getByRole("button", { name: "Close Why this shop?" }).click();
  await expect(page.getByRole("tab")).toHaveCount(0);
  await page.getByRole("button", { name: "Open in the side panel" }).click();
  await expect(
    page.getByRole("menuitem", { name: "Why this shop?" }),
  ).toBeVisible();
});

test("the goal pauses from its tab, and many tabs can all be found in one list", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: unknown[] = [];
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...task, goal_paused: true } });
  });
  const chats = Array.from({ length: 6 }, (_, i) => ({
    id: `m${i}`,
    thread_id: `c${i}`,
    question: `Question number ${i}`,
    answer: "An answer",
    error: null,
    created_at: task.created_at,
    answered_at: task.created_at,
  }));
  await page.route("**/api/v1/tasks/t1/side-chat", (route) =>
    route.fulfill({ json: chats }),
  );
  await page.addInitScript(() =>
    localStorage.setItem(
      "witx.panel.t1",
      JSON.stringify({
        tabs: [
          "phone",
          "goal",
          "chat:c0",
          "chat:c1",
          "chat:c2",
          "chat:c3",
          "chat:c4",
          "chat:c5",
        ],
        active: "goal",
      }),
    ),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Agent worked 1m 05s").first()).toBeVisible();
  await page.getByRole("button", { name: "Pause goal" }).click();
  await expect.poll(() => sent).toEqual([{ text: "/goal pause" }]);

  await page.getByRole("button", { name: "All open tabs" }).click();
  await page.getByRole("menuitem", { name: "Question number 5" }).click();
  await expect(
    page.getByRole("tab", { name: "Question number 5" }),
  ).toHaveAttribute("aria-selected", "true");
});

test("the goal tab shows the agent's plan and what it wrote down", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/tasks/t1/side-chat", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({
      json: {
        ...task,
        plan: [
          { step: "Turn on dark theme", status: "done" },
          { step: "Read the Android version", status: "in_progress" },
          { step: "Create the contact", status: "pending" },
        ],
        notes: [
          {
            text: "Android version 17",
            observation_id: "o1",
            seen_at: "2026-10-10T10:00:04Z",
          },
        ],
      },
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await page
    .getByRole("button", { name: /^Goal Buy the item in the cart/ })
    .click();

  const plan = page.getByRole("region", { name: "Plan" });
  await expect(plan.getByText("In progress:")).toBeAttached();
  await expect(plan.getByText("Read the Android version")).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Written down" })
      .getByText("Android version 17"),
  ).toBeVisible();
});
