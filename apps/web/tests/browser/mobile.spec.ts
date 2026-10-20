import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";

test("on a phone, a sheet springs back from a short drag and closes when dragged down", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, []);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.getByRole("button", { name: /^Model: gpt-6-luna/ }).click();
  const sheet = page.getByRole("dialog", { name: "Model" });
  await expect(sheet).toBeVisible();
  await page.waitForTimeout(400); // the sheet slides in before the handle stays put

  const drag = async (distance: number, ms: number) => {
    const bar = (await sheet.locator(":scope > div").first().boundingBox())!;
    const x = bar.x + bar.width / 2;
    const y = bar.y + 20;
    await page.mouse.move(x, y);
    await page.mouse.down();
    for (let i = 1; i <= 10; i++) {
      await page.mouse.move(x, y + (distance * i) / 10);
      await page.waitForTimeout(ms / 10);
    }
    await page.mouse.up();
  };
  await drag(30, 400); // slow and short: not a flick
  await expect(sheet).toBeVisible();
  await page.waitForTimeout(400); // the spring back (300 ms) moves the handle
  // Past a third of the sheet's height, staying inside the window.
  await drag((await sheet.boundingBox())!.height * 0.6, 0);
  await expect(sheet).toBeHidden();
});

test.describe("on a phone held sideways", () => {
  test.use({
    viewport: { width: 667, height: 375 },
    hasTouch: true,
    isMobile: true,
  });

  test("the navigation can be opened, and the message box sits at the bottom", async ({
    page,
  }) => {
    await mockApi(page, []);
    await page.goto("/sign-in");
    await page.getByLabel("Developer token").fill("dev-token");
    await page.getByRole("button", { name: "Sign in with token" }).click();
    const box = page.getByRole("textbox", { name: /goal|what/i }).first();
    await expect(box).toBeVisible();
    const form = (await page.locator("form").first().boundingBox())!;
    expect(375 - (form.y + form.height)).toBeLessThan(40);

    await page.getByRole("button", { name: "Open navigation" }).click();
    await expect(
      page.getByRole("complementary", { name: "Main navigation" }),
    ).toBeVisible();
  });
});
