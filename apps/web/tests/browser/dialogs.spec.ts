import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { signIn } from "./fixtures/auth";

async function openScheduleDialog(page: Page) {
  await mockApi(page, []);
  await page.route("**/api/v1/schedules", (route) =>
    route.fulfill({ json: [] }),
  );
  await signIn(page);
  await page.locator("#goal").fill("Check the battery");
  await page.getByRole("button", { name: "More options" }).click();
  await page.getByRole("menuitem", { name: "Run on a schedule…" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Run this task on a schedule",
  });
  await expect(dialog).toBeVisible();
  return dialog;
}

test("Tab and Shift+Tab keep focus inside an open dialog", async ({ page }) => {
  const dialog = await openScheduleDialog(page);
  const inside = () =>
    dialog.evaluate((el) => el.contains(document.activeElement));
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press("Tab");
    expect(await inside()).toBe(true);
  }
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press("Shift+Tab");
    expect(await inside()).toBe(true);
  }
});

test("on a phone, Escape closes only the sheet on top of a dialog", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const dialog = await openScheduleDialog(page);
  await dialog.getByRole("button", { name: /^Repeat:/ }).click();
  const sheet = page.getByRole("dialog", { name: "Repeat" });
  await expect(sheet).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});
