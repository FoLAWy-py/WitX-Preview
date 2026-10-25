import { expect, test, type Page } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";

const ROUNDS = 30;
const at = (n: number) =>
  new Date(Date.parse("2026-10-10T10:00:00Z") + n * 60_000).toISOString();

/** A finished task of many rounds, each ending in a result; the page holds the rounds
 * from the 11th on, more than fill the view, and the part before is not loaded yet.
 * Fetched again `from` its start, as the page does once it holds the part before (after
 * each reconnect of its event stream, say), the task comes whole, as the server sends it. */
function longTask(from: string | null) {
  const runs = Array.from({ length: ROUNDS }, (_, i) => ({
    ...task.runs[0],
    id: `r${i + 1}`,
    status: "completed",
    wait_reason: null,
    created_at: at(i * 2),
    result: {
      summary: `Round ${i + 1} is done.`,
      evidence_observation_ids: [],
      completed_at: at(i * 2 + 1),
    },
  }));
  return {
    ...task,
    status: "completed",
    actions: [],
    observations: [],
    runs,
    result: runs[ROUNDS - 1].result,
    earlier:
      from && Date.parse(from) <= Date.parse(task.created_at)
        ? null
        : { before: at(20) },
  };
}

const result = (page: Page, n: number) =>
  page.getByText(`Round ${n} is done.`, { exact: true });

/** Scrolls the conversation to its top until `done` (the page may still be moving to
 * its newest message as it opens, as a person would not have scrolled yet). */
async function scrollToTopUntil(page: Page, done: () => Promise<boolean>) {
  await expect
    .poll(async () => {
      await result(page, ROUNDS).evaluate((el) => {
        for (let at = el.parentElement; at; at = at.parentElement)
          if (getComputedStyle(at).overflowY === "auto")
            return at.scrollTo(0, 0);
      });
      return done();
    })
    .toBe(true);
}

test("a long task opens at its latest part, and the one before loads as the top nears view, keeping the view where it was", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({
      json: longTask(new URL(route.request().url()).searchParams.get("from")),
    }),
  );
  const asked: URL[] = [];
  // The part is sent once the view is left at the top: scrolled there again after the
  // part came in, it would no longer show where the page kept it.
  let send = () => {};
  const scrolled = new Promise<void>((resolve) => (send = resolve));
  await page.route("**/api/v1/tasks/t1/earlier*", async (route) => {
    asked.push(new URL(route.request().url()));
    await scrolled;
    return route.fulfill({
      json: {
        actions: [],
        observations: [],
        notes: [],
        events: [],
        earlier: null,
      },
    });
  });
  await signIn(page);
  await openTask(page);

  await expect(result(page, ROUNDS)).toBeVisible();
  await expect(result(page, 1)).toHaveCount(0);
  await expect(result(page, 11)).toHaveCount(1);

  await scrollToTopUntil(page, async () => asked.length > 0);
  send();

  await expect(result(page, 1)).toHaveCount(1);
  expect(asked[0].searchParams.get("before")).toBe(at(20));
  expect(asked[0].searchParams.get("types")).toContain("agent.message");
  // The part came in above what was in view: the round at the top is still in sight,
  // the rounds before it above the view, to scroll up to.
  await expect(result(page, 11)).toBeInViewport();
  await expect
    .poll(async () => (await result(page, 1).boundingBox())?.y ?? Infinity)
    .toBeLessThan(0);
});

test("a part that fails to load can be asked for again", async ({ page }) => {
  await mockApi(page, []);
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({
      json: longTask(new URL(route.request().url()).searchParams.get("from")),
    }),
  );
  let failing = true;
  await page.route("**/api/v1/tasks/t1/earlier*", (route) =>
    failing
      ? route.fulfill({ status: 500, json: {} })
      : route.fulfill({
          json: {
            actions: [],
            observations: [],
            notes: [],
            events: [],
            earlier: null,
          },
        }),
  );
  await signIn(page);
  await openTask(page);
  await expect(result(page, ROUNDS)).toBeVisible();

  const again = page.getByRole("button", { name: "Show earlier" });
  await scrollToTopUntil(page, () => again.isVisible());
  failing = false;
  await again.click();

  await expect(result(page, 1)).toHaveCount(1);
  await expect(again).toHaveCount(0);
});
