import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("a sub-agent is named in the conversation and opens in a tab of its own", async ({
  page,
}) => {
  await mockApi(page, []);
  const child = {
    ...task.runs[0],
    id: "r2",
    parent_run_id: "r1",
    name: "Android Agent",
    goal: "Turn the Pixel sideways and open google.com",
    status: "waiting",
    wait_reason: "user_input",
    device_ids: ["d1"],
  };
  const working = {
    ...task,
    actions: [],
    runs: [
      { ...task.runs[0], status: "waiting", wait_reason: "children" },
      child,
    ],
  };
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: working }));
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "agent.question",
        data: {
          text: "Chrome cannot reach google.com on d1",
          run_id: "r2",
        },
        created_at: "2026-10-10T10:00:06Z",
      })}\n\n`,
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Android Agent asks")).toBeVisible();
  await page.getByRole("button", { name: "Open Android Agent" }).click();
  await expect(page.getByRole("tab", { name: "Android Agent" })).toBeVisible();
  await expect(
    page.getByText("Turn the Pixel sideways and open google.com"),
  ).toBeVisible();
});

test("sub-agents' questions are answered one at a time, each to the agent that asked", async ({
  page,
}) => {
  await mockApi(page, []);
  const sub = (id: string, name: string) => ({
    ...task.runs[0],
    id,
    parent_run_id: "r1",
    name,
    goal: `${name}: sign in`,
    status: "waiting",
    wait_reason: "user_input",
    device_ids: [],
  });
  const asking = {
    ...task,
    actions: [],
    runs: [
      { ...task.runs[0], status: "waiting", wait_reason: "children" },
      sub("r2", "iOS Agent"),
      sub("r3", "Android Agent"),
    ],
  };
  const sent: Record<string, unknown>[] = [];
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: asking }));
  await page.route("**/api/v1/tasks/t1/inputs", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({ json: asking });
  });
  const question = (seq: number, runId: string, text: string, to = "user") =>
    `data: ${JSON.stringify({
      seq,
      type: "agent.question",
      data: { text, run_id: runId, to },
      created_at: `2026-10-10T10:00:0${seq}Z`,
    })}\n\n`;
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body:
        question(1, "r3", "Which Google account?") +
        question(2, "r2", "Which Apple ID?") +
        question(3, "r2", "Which iPhone model is it?", "main"),
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("iOS Agent asked the main agent:")).toBeVisible();
  await expect(page.getByText("1 of 2")).toBeVisible();
  await expect(page.getByText("Waiting its turn")).toBeVisible();
  await expect(
    page.getByText(
      "A sub-agent asks you above. A message here goes to the main agent",
    ),
  ).toBeVisible();
  await page.getByLabel("Type your answer").fill("work account");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect
    .poll(() => sent)
    .toEqual([{ text: "work account", run_id: "r3" }]);
});
