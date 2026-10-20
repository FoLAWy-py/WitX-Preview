import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("full access needs an explicit confirmation", async ({ page }) => {
  const modes: unknown[] = [];
  await mockApi(page, [], modes, true);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const choose = async () => {
    await page.getByRole("button", { name: /^Permission:/ }).click();
    await page.getByRole("option", { name: /Full access/ }).click();
  };
  await choose();
  const dialog = page.getByRole("dialog", { name: "Use full access?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  expect(modes).toEqual([]);

  await choose();
  await dialog.getByRole("button", { name: "Use full access" }).click();
  await expect.poll(() => modes).toEqual([{ permission_mode: "full" }]);
});

const deletion = {
  ...task.actions[0],
  target: "Delete photo",
  expected_effect: "The photo is deleted",
  effects: ["delete_data"],
  intent: null,
  policy: {
    ...task.actions[0].policy,
    reasons: ["risk check: deletes a photo"],
    risk: { ...task.actions[0].policy.risk, effects: ["delete_data"] },
  },
};

test("a decided approval stays as a line, and the tab's title counts what waits", async ({
  page,
}) => {
  await mockApi(page, []);
  const approved = {
    ...task,
    actions: [
      {
        ...task.actions[0],
        status: "done",
        effect: "unverified",
        approval: {
          status: "used",
          expires_at: task.actions[0].approval.expires_at,
          decided_at: "2026-10-10T10:00:30Z",
        },
      },
    ],
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: approved }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("You approved: Place order")).toBeVisible();
  await expect(page.getByText("¥42.50")).toBeVisible();
  await expect(page).toHaveTitle("(1) Buy the item in the cart · WitX");
});

test("similar steps are approved too only after a confirmation that says what it covers", async ({
  page,
}) => {
  const decisions: unknown[] = [];
  await mockApi(page, decisions);
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({ json: { ...task, actions: [deletion] } }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await page
    .getByRole("button", { name: "Approve, and similar steps too" })
    .click();
  const confirm = page.getByRole("dialog", {
    name: "Approve similar steps too?",
  });
  await expect(confirm.getByText(/up to 20 steps like this one/)).toBeVisible();
  await expect(confirm.getByText(/com\.shop/)).toBeVisible();
  expect(decisions).toEqual([]);
  await confirm.getByRole("button", { name: "Approve similar steps" }).click();
  await expect
    .poll(() => decisions)
    .toEqual([{ decision: "approve", similar: true }]);
});

test("a payment offers no approval of similar steps", async ({ page }) => {
  await mockApi(page, []);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(page.getByText("Corner Shop")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve, and similar steps too" }),
  ).toHaveCount(0);
});

test("a standing approval says how many steps it still covers and can be revoked", async ({
  page,
}) => {
  await mockApi(page, []);
  const revoked: string[] = [];
  const used = {
    status: "used",
    expires_at: task.actions[0].approval.expires_at,
    decided_at: "2026-10-10T10:00:30Z",
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({
      json: {
        ...task,
        runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
        actions: [
          { ...deletion, status: "done", effect: "unverified", approval: used },
          {
            ...deletion,
            id: "a2",
            target: "Delete second photo",
            status: "done",
            effect: "unverified",
            approval: { ...used, grant_id: "g1" },
          },
        ],
        grants: [
          {
            id: "g1",
            created_from: "a1",
            device_id: "d1",
            app: "com.shop",
            effects: ["delete_data"],
            terms: {},
            status: "active",
            uses: 3,
            max_uses: 20,
            expires_at: "2026-10-10T11:00:30Z",
            created_at: "2026-10-10T10:00:30Z",
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/grants/g1/revoke", (route) => {
    revoked.push("g1");
    return route.fulfill({ json: {} });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  await expect(
    page.getByText("Approved as a similar step: Delete second photo"),
  ).toBeVisible();
  await expect(page.getByText(/similar steps approved, 17 left/)).toBeVisible();
  await page.getByRole("button", { name: "Revoke" }).click();
  await expect.poll(() => revoked).toEqual(["g1"]);
});

test("an approval kept in the conversation opens to show what was approved", async ({
  page,
}) => {
  await mockApi(page, []);
  const approved = {
    ...task.actions[0],
    status: "done",
    effect: "unverified",
    approval: {
      status: "used",
      expires_at: task.actions[0].approval.expires_at,
      decided_at: "2026-10-10T10:00:30Z",
    },
  };
  await page.route("**/api/v1/tasks/t1", (route) =>
    route.fulfill({
      json: {
        ...task,
        runs: [{ ...task.runs[0], status: "running", wait_reason: null }],
        actions: [approved],
      },
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const record = page.getByRole("button", {
    name: /You approved: Place order/,
  });
  await expect(page.getByText("risk check: confirms the order")).toHaveCount(0);
  await record.click();
  await expect(record).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("risk check: confirms the order")).toBeVisible();
  await expect(page.getByText("Corner Shop")).toBeVisible();
  await page
    .getByRole("button", {
      name: "Chosen on this screen, with the input marked",
    })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
});
