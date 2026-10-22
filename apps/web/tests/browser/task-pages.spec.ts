import { expect, test, type Page, type Route } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";

function summary(n: number) {
  const name = `Errand ${String(n).padStart(2, "0")}`;
  return {
    id: `e${n}`,
    request: name,
    goal: "",
    title: name,
    status: "completed",
    permission_mode: "auto",
    needs_attention: false,
    group_id: null,
    pinned: false,
    created_at: task.created_at,
    updated_at: task.updated_at,
  };
}

/**
 * The tasks in no group, served as the API does: `limit` at a time, the next page at
 * the `Link` header's cursor (here, simply the index to go on from). Returns every
 * request for that list.
 */
async function serveErrands(
  page: Page,
  errands: ReturnType<typeof summary>[],
  fail?: (url: URL) => boolean,
) {
  const asked: URL[] = [];
  await page.route(
    (url) =>
      url.pathname === "/api/v1/tasks" &&
      url.searchParams.get("group") === "none",
    (route: Route) => {
      const url = new URL(route.request().url());
      asked.push(url);
      if (fail?.(url)) return route.fulfill({ status: 500, json: {} });
      const limit = Number(url.searchParams.get("limit") ?? 100);
      const from = Number(url.searchParams.get("cursor") ?? 0);
      const shown = errands.slice(from, from + limit);
      const next = new URLSearchParams(url.searchParams);
      next.set("cursor", String(from + limit));
      const more = from + limit < errands.length;
      return route.fulfill({
        json: shown,
        headers: more
          ? { Link: `</api/v1/tasks?${next.toString()}>; rel="next"` }
          : {},
      });
    },
  );
  return asked;
}

const errand = (page: Page, n: number) =>
  page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: summary(n).title, exact: true });

async function scrollToEnd(page: Page) {
  await page
    .getByRole("navigation", { name: "Main" })
    .evaluate((nav) => nav.scrollTo(0, nav.scrollHeight));
}

test("the task list loads its next page as it is scrolled to the end, a page at a time", async ({
  page,
}) => {
  await mockApi(page, []);
  const errands = Array.from({ length: 70 }, (_, n) => summary(n + 1));
  const asked = await serveErrands(page, errands);
  await signIn(page);
  await expect(errand(page, 1)).toBeVisible();

  for (let n = 0; n < 4 && !(await errand(page, 70).count()); n++) {
    await scrollToEnd(page);
    await page.waitForTimeout(100);
  }

  await expect(errand(page, 70)).toBeVisible();
  // The first page, then each next one from where the list ended: never all again.
  const pages = asked.map((url) => url.searchParams.get("cursor"));
  expect(pages).toEqual([null, "20", "40", "60"]);
  expect(asked.every((url) => url.searchParams.get("limit") === "20")).toBe(
    true,
  );
});

test("a list loaded again keeps the tasks scrolled to and drops one deleted elsewhere", async ({
  page,
}) => {
  await mockApi(page, []);
  const errands = Array.from({ length: 30 }, (_, n) => summary(n + 1));
  const asked = await serveErrands(page, errands);
  await signIn(page);
  await expect(errand(page, 1)).toBeVisible();
  await scrollToEnd(page);
  await expect(errand(page, 30)).toBeVisible();

  errands.splice(24, 1); // errand 25 is deleted on another device
  const before = asked.length;
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );

  await expect(errand(page, 25)).toHaveCount(0);
  await expect(errand(page, 30)).toBeVisible();
  // One request for everything shown, not one per page.
  expect(
    asked.slice(before).map((url) => url.searchParams.get("limit")),
  ).toEqual(["30"]);
});

test("a next page that fails to load can be asked for again", async ({
  page,
}) => {
  await mockApi(page, []);
  const errands = Array.from({ length: 25 }, (_, n) => summary(n + 1));
  let failing = true;
  await serveErrands(
    page,
    errands,
    (url) => failing && url.searchParams.get("cursor") === "20",
  );
  await signIn(page);
  await expect(errand(page, 1)).toBeVisible();
  await scrollToEnd(page);

  const again = page.getByRole("button", { name: "Show more" });
  await expect(again).toBeVisible();
  failing = false;
  await again.click();

  await expect(errand(page, 25)).toBeVisible();
});
