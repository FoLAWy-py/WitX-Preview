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

test("a task opened from another follows its own events from its own last event", async ({
  page,
}) => {
  await mockApi(page, []);
  const summary = (id: string, title: string) => ({
    id,
    request: title,
    goal: "",
    title,
    status: "active",
    permission_mode: "auto",
    needs_attention: false,
    group_id: null,
    pinned: false,
    created_at: task.created_at,
    updated_at: task.updated_at,
  });
  await page.route("**/api/v1/tasks?*", (route) =>
    route.fulfill({
      json: [summary("t1", "Open Notes"), summary("t2", "Check battery")],
    }),
  );
  const seqs: Record<string, number> = { t1: 40, t2: 3 };
  for (const id of ["t1", "t2"])
    await page.route(detailOf(id), (route) =>
      route.fulfill({ json: { ...task, id, last_event_seq: seqs[id] } }),
    );
  const streams: string[] = [];
  await page.route(
    (url) => /^\/api\/v1\/tasks\/t[12]\/events$/.test(url.pathname),
    (route) => {
      const url = new URL(route.request().url());
      streams.push(
        `${url.pathname.split("/")[4]}@${url.searchParams.get("after")}`,
      );
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    },
  );
  await signIn(page);

  const nav = page.getByRole("navigation");
  await nav.getByRole("link", { name: "Open Notes" }).click();
  await expect.poll(() => streams).toContain("t1@40");
  await nav.getByRole("link", { name: "Check battery" }).click();
  await expect
    .poll(() => streams.filter((s) => s.startsWith("t2")))
    .not.toEqual([]);
  expect(streams.find((s) => s.startsWith("t2"))).toBe("t2@3");
});
