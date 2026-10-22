import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { PNG } from "./fixtures/media";
import { task } from "./fixtures/task";

const ROUNDS = 30;

/** A finished task of many rounds, each ending in a result that cites a screenshot of its
 * own: o1 in the first round, at the top, up to o30 in the last. */
function longTask() {
  const at = (n: number) =>
    new Date(Date.parse("2026-10-10T10:00:00Z") + n * 60_000).toISOString();
  const runs = Array.from({ length: ROUNDS }, (_, i) => ({
    ...task.runs[0],
    id: `r${i + 1}`,
    status: "completed",
    wait_reason: null,
    created_at: at(i * 2),
    result: {
      summary: `Round ${i + 1} is done.`,
      evidence_observation_ids: [`o${i + 1}`],
      completed_at: at(i * 2 + 1),
    },
  }));
  return {
    ...task,
    status: "completed",
    actions: [],
    runs,
    observations: runs.map((_, i) => ({
      ...task.observations[0],
      id: `o${i + 1}`,
      captured_at: at(i * 2 + 1),
    })),
    result: runs[ROUNDS - 1].result,
  };
}

test("a conversation's screenshots load as they come near view, not all at once", async ({
  page,
}) => {
  await mockApi(page, []);
  const long = longTask();
  await page.route(detailOf("t1"), (route) => route.fulfill({ json: long }));
  const asked = new Set<string>();
  await page.route("**/api/v1/observations/*/screenshot*", (route) => {
    asked.add(new URL(route.request().url()).pathname.split("/")[4]);
    return route.fulfill({ contentType: "image/png", body: PNG });
  });
  await signIn(page);
  await openTask(page);

  // The conversation opens at its end: the last round's picture loads, the first's not.
  await expect(page.getByText(`Round ${ROUNDS} is done.`)).toBeVisible();
  await expect.poll(() => asked.has(`o${ROUNDS}`)).toBe(true);
  expect(asked.has("o1")).toBe(false);
  expect(asked.size).toBeLessThan(ROUNDS);

  await page.getByText("Round 1 is done.").scrollIntoViewIfNeeded();

  await expect.poll(() => asked.has("o1")).toBe(true);
});
