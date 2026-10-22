import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("after a burst of events the page asks only for what changed since its last event", async ({
  page,
}) => {
  await mockApi(page, []);
  const running = {
    ...task,
    actions: [],
    runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
    last_event_seq: 5,
  };
  const result = {
    summary: "The cart is paid for.",
    evidence_observation_ids: [],
    completed_at: "2026-10-10T10:05:00Z",
  };
  const whole: string[] = [];
  await page.route(detailOf("t1"), (route) => {
    whole.push(route.request().url());
    return route.fulfill({ json: running });
  });
  const asked: string[] = [];
  await page.route("**/api/v1/tasks/t1/changes*", (route) => {
    asked.push(new URL(route.request().url()).search);
    return route.fulfill({
      json: {
        ...running,
        status: "completed",
        runs: [{ ...running.runs[0], status: "completed", result }],
        result,
        last_event_seq: 6,
        since: 5,
      },
    });
  });
  const streams: string[] = [];
  await page.route("**/api/v1/tasks/t1/events*", (route) => {
    const url = new URL(route.request().url());
    streams.push(url.searchParams.get("after") ?? "");
    return route.fulfill({
      contentType: "text/event-stream",
      body:
        url.searchParams.get("after") === "5"
          ? `id: 6\nevent: run.status\ndata: ${JSON.stringify({
              seq: 6,
              type: "run.status",
              data: { run_id: "r1", status: "completed" },
              created_at: "2026-10-10T10:05:00Z",
            })}\n\n`
          : "",
    });
  });
  await signIn(page);
  await openTask(page);

  await expect(page.getByText("The cart is paid for.")).toBeVisible();
  const query = new URLSearchParams(asked[0]);
  expect(query.get("since")).toBe("5");
  expect(query.get("open")).toBe("r1"); // the run it holds as going on
  expect(whole).toHaveLength(1); // only when the page opened
  // The conversation's events come with the task, of the kinds it shows; the stream
  // brings only what happens after it.
  const kinds = new URL(whole[0]).searchParams.get("types")!.split(",");
  expect(kinds).toContain("agent.message");
  expect(kinds).not.toContain("run.status");
  expect(streams[0]).toBe("5");
});
