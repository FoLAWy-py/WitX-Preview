import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { task } from "./fixtures/task";

test("what the agent remembers on its own shows in the conversation and can be undone", async ({
  page,
}) => {
  await mockApi(page, []);
  const undone: string[] = [];
  await page.route(detailOf("t1"), (route) =>
    route.fulfill({
      json: {
        ...task,
        actions: [],
        memories: [
          {
            id: "m1",
            text: "The harvest button is right of the crop",
            kind: "app",
            app: "com.farm.game",
            source_task_id: "t1",
            created_at: "2026-10-10T10:00:04Z",
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/memories/m1", (route) => {
    undone.push(route.request().method());
    return route.fulfill({ status: 204 });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const line = page.getByRole("note", { name: "What the agent remembered" });
  await expect(
    line.getByText("Remembered (how com.farm.game works):"),
  ).toBeVisible();
  await expect(
    line.getByText("The harvest button is right of the crop"),
  ).toBeVisible();
  await line.getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => undone).toEqual(["DELETE"]);
});

test("a person adds and forgets what the agent remembers, and can turn its remembering off", async ({
  page,
}) => {
  await mockApi(page, []);
  const added: unknown[] = [];
  const forgotten: string[] = [];
  await page.route("**/api/v1/memories", (route) => {
    if (route.request().method() === "POST") {
      added.push(route.request().postDataJSON());
      return route.fulfill({ status: 201, json: {} });
    }
    return route.fulfill({
      json: [
        {
          id: "m1",
          text: "I live in Shanghai",
          kind: "user",
          source_task_id: null,
          created_at: "2026-10-10T10:00:04Z",
        },
      ],
    });
  });
  await page.route("**/api/v1/memories/m1", (route) => {
    forgotten.push(route.request().method());
    return route.fulfill({ status: 204 });
  });
  const settings: unknown[] = [];
  await page.route("**/api/v1/memories/settings", (route) => {
    settings.push(route.request().postDataJSON());
    return route.fulfill({ json: { auto_memory: false } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/settings");

  await expect(page.getByText("I live in Shanghai")).toBeVisible();
  await page
    .getByLabel("What the agent remembers for you")
    .fill("I order food on Meituan");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect.poll(() => added).toEqual([{ text: "I order food on Meituan" }]);
  await page.getByRole("button", { name: "Forget" }).click();
  await expect.poll(() => forgotten).toEqual(["DELETE"]);
  await page
    .getByRole("switch", { name: "Let the agent remember on its own" })
    .click();
  await expect.poll(() => settings).toEqual([{ auto_memory: false }]);
});
