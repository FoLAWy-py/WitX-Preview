import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { signIn } from "./fixtures/auth";
import { scheduleView } from "./fixtures/schedule";
import { task } from "./fixtures/task";

test("a task is filed in a new group from the sidebar", async ({ page }) => {
  await mockApi(page, []);
  const groups: { id: string; name: string; created_at: string }[] = [];
  const moves: unknown[] = [];
  await page.route("**/api/v1/task-groups", async (route) => {
    if (route.request().method() === "POST") {
      const group = {
        id: "g1",
        name: (route.request().postDataJSON() as { name: string }).name,
        created_at: "2026-10-10T10:00:00Z",
      };
      groups.push(group);
      return route.fulfill({ status: 201, json: group });
    }
    return route.fulfill({ json: groups });
  });
  await page.route("**/api/v1/tasks/t1/group", (route) => {
    moves.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...task, group_id: "g1" } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  await page
    .getByRole("button", { name: "Actions for Buy the item in the cart" })
    .first()
    .click();
  await page
    .getByRole("menuitem", { name: "New group with this task…" })
    .click();
  const dialog = page.getByRole("dialog", { name: "New group" });
  await dialog.getByLabel("Name").fill("Shopping");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect.poll(() => moves).toEqual([{ group_id: "g1" }]);
  await expect(
    page.getByRole("button", { name: "Shopping", exact: true }),
  ).toBeVisible();
});

test("tasks are listed by their short title, and the sidebar leads to the schedules page, where one is started", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/tasks?*", (route) =>
    route.fulfill({
      json: [
        {
          id: "t1",
          request: "Buy the item in the cart",
          goal: "",
          title: "Buy cart item",
          status: "active",
          permission_mode: "auto",
          needs_attention: false,
          group_id: null,
          created_at: task.created_at,
          updated_at: task.updated_at,
        },
      ],
    }),
  );
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  const row = page
    .getByRole("navigation")
    .getByRole("link", { name: /Buy cart item/ });
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute("title", "Buy the item in the cart");
  await page.getByRole("link", { name: "Scheduled" }).click();
  await expect(page).toHaveURL(/\/schedules$/);
  await expect(page.getByText("No schedules yet")).toBeVisible();
  await page.getByRole("button", { name: "New schedule" }).first().click();
  await expect(
    page.getByRole("dialog", { name: "Run this task on a schedule" }),
  ).toBeVisible();
});

test("the schedules row, phone and task titles in the sidebar start on one vertical line", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({
      json: [
        scheduleView("s1", "Open Notes", {
          last_task_id: "t1",
          last_outcome: "started",
          last_run_at: "2026-10-16T00:00:00Z",
        }),
        scheduleView("s2", "Check the battery"),
      ],
    }),
  );
  await signIn(page);

  const nav = page.getByRole("navigation");
  const left = async (name: string) =>
    (await nav.getByText(name, { exact: true }).boundingBox())!.x;
  const scheduled = page
    .getByRole("link", { name: "Scheduled" })
    .getByText("Scheduled", { exact: true });
  await expect(scheduled).toBeVisible();
  await expect(nav.getByText("Pixel 8", { exact: true })).toBeVisible();
  const task = await left("Buy the item in the cart");
  expect((await scheduled.boundingBox())!.x).toBe(task);
  expect(await left("Pixel 8")).toBe(task);
  // Search shares the column too.
  expect((await page.getByPlaceholder("Search tasks").boundingBox())!.x).toBe(
    task,
  );
});

test("the sidebar offers ready phones first, six at a time", async ({
  page,
}) => {
  await mockApi(page, []);
  const phone = (name: string, patch: object = {}) => ({
    id: name,
    name,
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: null,
    pool: "Desk Mac",
    problems: [],
    enabled: true,
    ...patch,
  });
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({
      json: [
        phone("A offline", { online: false, problems: ["disconnected"] }),
        phone("B busy", { task_id: "t1" }),
        phone("C off", { enabled: false }),
        ...["D", "E", "F", "G", "H"].map((n) => phone(`${n} ready`)),
      ],
    }),
  );
  await signIn(page);

  const section = page.getByRole("navigation").locator("section").first();
  const names = section.getByRole("listitem");
  await expect(names).toHaveCount(6);
  await expect(names.first()).toHaveText("D ready");
  await expect(names.nth(5)).toHaveText("B busy"); // after the five ready ones
  await section.getByRole("button", { name: "Show 2 more" }).click();
  await expect(names).toHaveCount(8);
  await expect(names.last()).toHaveText("C off"); // not enabled: last
  await section.getByRole("button", { name: "Show less" }).click();
  await expect(names).toHaveCount(6);
});

test("a pinned task moves to Pinned, above the groups, and back when unpinned", async ({
  page,
}) => {
  await mockApi(page, []);
  const pinned = new Set<string>();
  const summary = (id: string, title: string) => ({
    id,
    request: title,
    goal: "",
    title,
    status: "completed",
    permission_mode: "auto",
    needs_attention: false,
    group_id: null,
    pinned: pinned.has(id),
    created_at: task.created_at,
    updated_at: task.updated_at,
  });
  await page.route("**/api/v1/tasks?*", (route) => {
    const which = new URL(route.request().url()).searchParams.get("pinned");
    const all = [summary("t1", "Open Notes"), summary("t2", "Check battery")];
    return route.fulfill({
      json: all.filter((t) =>
        which === "only"
          ? pinned.has(t.id)
          : which === "without"
            ? !pinned.has(t.id)
            : true,
      ),
    });
  });
  await page.route("**/api/v1/tasks/*/pin", (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[4];
    if (route.request().method() === "PUT") pinned.add(id);
    else pinned.delete(id);
    return route.fulfill({ status: 204 });
  });
  await signIn(page);

  const nav = page.getByRole("navigation");
  await expect(nav.getByText("Pinned", { exact: true })).toHaveCount(0);
  await nav.getByRole("link", { name: "Check battery" }).hover();
  await nav.getByRole("button", { name: "Actions for Check battery" }).click();
  await page.getByRole("menuitem", { name: "Pin" }).click();

  const section = nav.locator("section", { hasText: "Pinned" });
  await expect(
    section.getByRole("link", { name: "Check battery" }),
  ).toBeVisible();
  // Listed once: no longer among the other tasks.
  await expect(nav.getByRole("link", { name: "Check battery" })).toHaveCount(1);

  await section.getByRole("link", { name: "Check battery" }).hover();
  await section
    .getByRole("button", { name: "Actions for Check battery" })
    .click();
  await page.getByRole("menuitem", { name: "Unpin" }).click();
  await expect(nav.getByText("Pinned", { exact: true })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Check battery" })).toHaveCount(1);
});
