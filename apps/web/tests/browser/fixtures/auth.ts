import { type Page } from "@playwright/test";

/** Opens the task from the sidebar (the home page lists tasks waiting for you too). */
export async function openTask(page: Page) {
  await page
    .getByRole("navigation")
    .getByRole("link", { name: /Buy the item in the cart/ })
    .click();
}

export async function signIn(page: Page) {
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
}
