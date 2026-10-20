import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";

test("sign in, open a task and approve a sensitive action", async ({
  page,
}) => {
  const decisions: unknown[] = [];
  await mockApi(page, decisions);
  await page.goto("/");
  await expect(page).toHaveURL(/sign-in/);
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();

  await expect(page.getByText("Alice", { exact: true })).toBeVisible();
  await openTask(page);
  await expect(page.getByText("Purchase").first()).toBeVisible();
  // Why it needs approval is one click away, behind the terms.
  await page.getByText("Why this needs you").click();
  await expect(page.getByText("risk check: confirms the order")).toBeVisible();
  // The approval shows the terms it covers: who is paid how much.
  await expect(page.getByText("Corner Shop")).toBeVisible();
  await expect(page.getByText("¥42.50", { exact: true })).toBeVisible();
  await expect(page.getByText("1 Harbour Road, Flat 12")).toBeVisible();
  // The screen it was chosen on is a thumbnail that opens at full size.
  await page
    .getByRole("button", {
      name: "Chosen on this screen, with the input marked",
    })
    .click();
  await expect(
    page.getByRole("dialog").getByRole("img", {
      name: "Screenshot, with the input marked",
    }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  // A payment is approved only after typing its amount and confirming once more.
  await expect(
    page.getByLabel("Type the amount 42.50 to confirm all the terms above"),
  ).toHaveAttribute("placeholder", "42.50");
  await page.getByLabel(/to confirm all the terms above/).fill("42.50");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  // One confirmation covers payee, address, payment method and amount together.
  const confirm = page.getByRole("dialog", { name: "Approve this payment?" });
  await expect(confirm.getByText("1 Harbour Road, Flat 12")).toBeVisible();
  await expect(confirm.getByText(/Not on this screen/)).toBeVisible(); // no payment method
  // Confirmed by sliding to the end (End on the keyboard), not by one click.
  await confirm
    .getByRole("slider", { name: "Slide to approve payment" })
    .press("End");
  await expect.poll(() => decisions).toEqual([{ decision: "approve" }]);
  await expect(confirm).toBeHidden();
});

test("a wrong token returns to sign-in", async ({ page }) => {
  await mockApi(page, []);
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("wrong");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await expect(page).toHaveURL(/sign-in/);
});
