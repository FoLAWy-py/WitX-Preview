import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";

test("the fast risk check is turned on only after its risk is accepted", async ({
  page,
}) => {
  await mockApi(page, []);
  const changes: unknown[] = [];
  await page.route("**/api/v1/tenant/settings", (route) => {
    changes.push(route.request().postDataJSON());
    return route.fulfill({
      json: { allow_full_access: false, fast_risk_check: true },
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/settings");

  const toggle = page.getByRole("switch", {
    name: "Fast risk check (Decisions API)",
  });
  await expect(page.getByText("Beta", { exact: true })).toBeVisible();
  await toggle.click();
  const warning = page.getByRole("dialog", {
    name: "Turn on the fast risk check?",
  });
  await expect(warning.getByText(/can be confidently wrong/)).toBeVisible();
  await warning.getByRole("button", { name: "Cancel" }).click();
  expect(changes).toEqual([]);

  await toggle.click();
  await warning
    .getByRole("button", { name: "Turn on, I accept the risk" })
    .click();
  await expect.poll(() => changes).toEqual([{ fast_risk_check: true }]);
});

test("an owner chooses which model providers receive the workspace's data", async ({
  page,
}) => {
  await mockApi(page, []);
  const changes: unknown[] = [];
  await page.route("**/api/v1/models", (route) =>
    route.fulfill({
      json: [
        {
          name: "gpt-6-luna",
          provider: "openai",
          default: true,
          allowed: true,
        },
        {
          name: "deepseek-v4.1-flash",
          provider: "deepinfra",
          default: false,
          allowed: true,
        },
      ],
    }),
  );
  await page.route("**/api/v1/tenant/settings", (route) => {
    changes.push(route.request().postDataJSON());
    return route.fulfill({ json: {} });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/settings");

  await expect(page.getByText("Model providers")).toBeVisible();
  await page.getByRole("switch", { name: "deepinfra" }).click();
  await expect.poll(() => changes).toEqual([{ allowed_providers: ["openai"] }]);
});

test("settings are in sections and a search keeps only the ones that match", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/settings");

  await expect(
    page.getByRole("heading", { name: "Access and safety" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Models", exact: true }),
  ).toBeVisible();
  await page.getByPlaceholder("Search settings").fill("switch models");
  await expect(
    page.getByRole("switch", { name: "Switch models when one keeps failing" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Access and safety" }),
  ).toHaveCount(0);
  await page.getByPlaceholder("Search settings").fill("zzz");
  await expect(page.getByText("No settings match “zzz”")).toBeVisible();
});

test("a person chooses how long their screenshots are kept, up to the deployment's most", async ({
  page,
}) => {
  await mockApi(page, []);
  const chosen: unknown[] = [];
  await page.route("**/api/v1/me/retention", (route) => {
    chosen.push(route.request().postDataJSON());
    return route.fulfill({ json: { screenshot_retention_days: 7 } });
  });
  await page.route("**/api/v1/me", (route) =>
    route.fulfill({
      json: {
        user_id: "u",
        display_name: "Alice",
        tenant_id: "x",
        tenant_name: "Acme",
        role: "member",
        allow_full_access: false,
        fast_risk_check: false,
        allowed_providers: ["openai"],
        model_fallback: false,
        auto_memory: true,
        screenshot_retention_days: 14,
        screenshot_retention_max: 14, // this deployment keeps them two weeks at most
        audit_retention_days: 90,
      },
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/settings");

  const keep = page.getByRole("radiogroup", { name: "Keep my screenshots" });
  await expect(keep.getByRole("radio", { name: "14 days" })).toBeChecked();
  await expect(keep.getByRole("radio", { name: "30 days" })).toHaveCount(0);
  await keep.getByRole("radio", { name: "7 days" }).click();
  await expect.poll(() => chosen).toEqual([{ screenshot_retention_days: 7 }]);
});
