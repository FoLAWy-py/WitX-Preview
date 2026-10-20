import { expect, type Page } from "@playwright/test";
import { openTask } from "./auth";
import { SCREEN_JPEG } from "./media";

type PushedFrame = { frame_id: number; width: number; height: number };

/** A takeover whose mock server pushes pictures when the test says so, and answers
 * every input as carried out. */
export async function takeoverWithPictures(page: Page) {
  const sent: Record<string, unknown>[] = [];
  let push: (frames: PushedFrame[]) => void = () => {};
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    push = (frames) => {
      for (const frame of frames)
        ws.send(
          JSON.stringify({
            type: "frame",
            rotation: 0,
            jpeg: SCREEN_JPEG,
            ...frame,
          }),
        );
    };
    push([{ frame_id: 1, width: 1080, height: 2400 }]);
    ws.onMessage((raw) => {
      const message = JSON.parse(String(raw)) as Record<string, unknown>;
      sent.push(message);
      if (message.type === "input")
        ws.send(
          JSON.stringify({
            type: "input_result",
            input_id: message.input_id,
            outcome: "returned",
            reason: null,
          }),
        );
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await page.getByRole("button", { name: "Take over" }).click();
  const screen = page
    .getByRole("region", { name: "Controlling Pixel 8" })
    .getByRole("img", { name: "Live screen of Pixel 8" });
  await expect(screen).toBeVisible();
  return { sent, screen, push: (frames: PushedFrame[]) => push(frames) };
}
