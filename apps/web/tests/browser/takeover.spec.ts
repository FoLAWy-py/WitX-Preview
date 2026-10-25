import { expect, test } from "@playwright/test";
import { detailOf, mockApi } from "./fixtures/api";
import { openTask, signIn } from "./fixtures/auth";
import { task } from "./fixtures/task";
import { SCREEN_JPEG } from "./fixtures/media";
import { takeoverWithPictures } from "./fixtures/takeover";

test("taking over pauses the task, taps where clicked and hands back", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: Record<string, unknown>[] = [];
  let sockets = 0;
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    sockets += 1;
    // The session cookie signs the socket in: the picture starts at once.
    ws.send(
      JSON.stringify({
        type: "frame",
        frame_id: 1,
        width: 1080,
        height: 2400,
        jpeg: SCREEN_JPEG,
      }),
    );
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
      if (message.type === "end") {
        ws.send(JSON.stringify({ type: "ended", reason: "ended" }));
        void ws.close();
      }
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await page.getByRole("button", { name: "Take over" }).click();

  // The phone is controlled next to the conversation, which stays on the page.
  const dialog = page.getByRole("region", { name: "Controlling Pixel 8" });
  const screen = dialog.getByRole("img", { name: "Live screen of Pixel 8" });
  await expect(screen).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Buy the item in the cart" }),
  ).toBeVisible();
  // The server ends a takeover when its socket closes: the page opens exactly one.
  expect(sockets).toBe(1);
  await screen.click(); // the centre of the picture
  await expect
    .poll(() => sent.filter((m) => m.type === "input"))
    .toHaveLength(1);
  expect(sent.some((m) => m.type === "auth")).toBe(false); // no token in the page
  const tap = sent.find((m) => m.type === "input") as {
    input: { kind: string; x: number; y: number };
  };
  expect(tap.input.kind).toBe("tap");
  expect(Math.abs(tap.input.x - 540)).toBeLessThanOrEqual(12);
  expect(Math.abs(tap.input.y - 1200)).toBeLessThanOrEqual(12);

  await dialog.getByLabel("Type on the phone").fill("hunter2");
  await dialog.getByRole("button", { name: "Send text" }).click();
  await expect
    .poll(() => sent.filter((m) => m.type === "input").map((m) => m.input))
    .toContainEqual({ kind: "type_text", text: "hunter2" });

  await dialog.getByRole("button", { name: "Hand back and resume" }).click();
  await expect.poll(() => sent.at(-1)).toEqual({ type: "end", resume: true });
  await expect(dialog).toBeHidden();
});

test("leaving the page while the task is still pausing does not take the phone over", async ({
  page,
}) => {
  await mockApi(page, []);
  let status = "active";
  let polled = 0;
  await page.route(detailOf("t1"), (route) => {
    polled += 1;
    return route.fulfill({ json: { ...task, status } });
  });
  await page.route("**/api/v1/tasks/t1/pause", (route) => {
    status = "pausing"; // the phone has not confirmed yet
    return route.fulfill({ json: { ...task, status } });
  });
  const started: string[] = [];
  await page.route("**/api/v1/tasks/t1/takeover", (route) => {
    started.push(route.request().method());
    return route.fallback();
  });
  await signIn(page);
  await openTask(page);
  await page.getByRole("button", { name: "Take over" }).click();
  const before = polled;
  await expect.poll(() => polled).toBeGreaterThan(before); // waiting for the pause

  await page.getByRole("link", { name: "Scheduled" }).click();
  await expect(page).toHaveURL(/\/schedules/);
  status = "paused"; // the pause completes after the person left
  await page.waitForTimeout(1500);
  expect(started).toEqual([]);
});

test("a takeover that gets no picture from the phone ends saying it stopped responding", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    ws.send(JSON.stringify({ type: "ended", reason: "device_not_responding" }));
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await page.getByRole("button", { name: "Take over" }).click();

  const note = page
    .getByRole("alert")
    .filter({ hasText: "stopped responding" });
  await expect(note).toContainText(
    "The phone stopped responding: no picture came, so the takeover ended.",
  );
  await expect(note).toContainText("The task stays paused.");
});

test("takeover pictures are drawn on a canvas and input refers to the newest one drawn", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: Record<string, unknown>[] = [];
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    // Three pictures at once: the page keeps the newest.
    for (const frame_id of [1, 2, 3])
      ws.send(
        JSON.stringify({
          type: "frame",
          frame_id,
          width: 1080,
          height: 2400,
          jpeg: SCREEN_JPEG,
        }),
      );
    ws.onMessage((raw) => sent.push(JSON.parse(String(raw))));
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
  await page.getByRole("button", { name: "Take over" }).click();

  const screen = page.getByRole("img", { name: "Live screen of Pixel 8" });
  await expect(screen).toBeVisible();
  await expect
    .poll(() =>
      screen.evaluate((el: HTMLCanvasElement) => {
        const data = el
          .getContext("2d")!
          .getImageData(el.width / 2, el.height / 2, 1, 1).data;
        return data[3]; // the picture is drawn: its pixels are opaque
      }),
    )
    .toBe(255);
  await screen.click();
  await expect
    .poll(() => sent.find((m) => m.type === "input"))
    .toMatchObject({ frame_id: 3 });
});

test("a free phone is taken over right in the device pool; one in a task on its task page", async ({
  page,
}) => {
  await mockApi(page, []);
  const pixel = {
    id: "d1",
    name: "Pixel 8",
    platform: "android",
    online: true,
    capabilities: ["tap"],
    task_id: "t1",
    task_goal: "Buy the item in the cart",
    pool: "Desk Mac",
    problems: [],
    enabled: true,
    taken_over: false,
  };
  const galaxy = {
    ...pixel,
    id: "d2",
    name: "Galaxy S30",
    task_id: null,
    task_goal: null,
  };
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({ json: [pixel, galaxy] }),
  );
  const started: string[] = [];
  await page.route("**/api/v1/devices/*/takeover", (route) => {
    started.push(new URL(route.request().url()).pathname);
    return route.fulfill({
      status: 201,
      json: {
        takeover_id: "k1",
        device_id: "d2",
        expires_at: new Date(Date.now() + 120_000).toISOString(),
      },
    });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.goto("/devices");

  const card = (name: string) =>
    page.locator("div.scroll-mt-20", {
      has: page.getByRole("heading", { name }),
    });
  await card("Galaxy S30").getByRole("button", { name: "Take over" }).click();
  await expect(
    page.getByRole("dialog", { name: "Controlling Galaxy S30" }),
  ).toBeVisible();
  expect(started).toEqual(["/api/v1/devices/d2/takeover"]);
  await page
    .getByRole("button", { name: /^(Back|End the takeover and go back)$/ })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Controlling Galaxy S30" }),
  ).toBeHidden();

  await page.goto("/devices");
  await card("Pixel 8").getByRole("button", { name: "Take over" }).click();
  await expect(page).toHaveURL(/\/tasks\/t1/);
  expect(started).toEqual(["/api/v1/devices/d2/takeover"]);
});

test("in a takeover, a click held on the screen is a long press", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: Record<string, unknown>[] = [];
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    ws.send(
      JSON.stringify({
        type: "frame",
        frame_id: 1,
        width: 1080,
        height: 2400,
        jpeg: SCREEN_JPEG,
      }),
    );
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
  const box = (await screen.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(700);
  await page.mouse.up();
  await expect
    .poll(() => sent.filter((m) => m.type === "input"))
    .toHaveLength(1);
  const press = sent.find((m) => m.type === "input") as {
    input: { kind: string; x: number; y: number; duration_ms: number };
  };
  expect(press.input.kind).toBe("long_press");
  expect(press.input.duration_ms).toBeGreaterThanOrEqual(500);
  expect(Math.abs(press.input.x - 540)).toBeLessThanOrEqual(12);
  expect(Math.abs(press.input.y - 1200)).toBeLessThanOrEqual(12);
});

test("in a takeover, a dragged curve is sent with its path and drawn on the picture", async ({
  page,
}) => {
  await mockApi(page, []);
  const sent: Record<string, unknown>[] = [];
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    ws.send(
      JSON.stringify({
        type: "frame",
        frame_id: 1,
        width: 1080,
        height: 2400,
        jpeg: SCREEN_JPEG,
      }),
    );
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
  const box = (await screen.boundingBox())!;
  // An arc from the left of the picture to the right, bulging upwards.
  const x0 = box.x + box.width * 0.2;
  const y0 = box.y + box.height * 0.5;
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  for (let i = 1; i <= 12; i += 1) {
    const f = i / 12;
    await page.mouse.move(
      x0 + f * box.width * 0.6,
      y0 - Math.sin(f * Math.PI) * box.height * 0.15,
    );
    await page.waitForTimeout(25);
  }
  await page.mouse.up();
  await expect
    .poll(() => sent.filter((m) => m.type === "input"))
    .toHaveLength(1);
  const swipe = sent.find((m) => m.type === "input") as {
    input: {
      kind: string;
      x1: number;
      y1: number;
      x2: number;
      y2: number;
      duration_ms: number;
      path: { x: number; y: number; t_ms: number }[];
    };
  };
  expect(swipe.input.kind).toBe("swipe");
  const path = swipe.input.path;
  expect(path.length).toBeGreaterThan(2);
  expect(path[0]).toMatchObject({
    x: swipe.input.x1,
    y: swipe.input.y1,
    t_ms: 0,
  });
  expect(path[path.length - 1]).toMatchObject({
    x: swipe.input.x2,
    y: swipe.input.y2,
    t_ms: swipe.input.duration_ms,
  });
  // The way bent: its middle is well above the line between its ends.
  const middle = path[Math.floor(path.length / 2)];
  expect(middle.y).toBeLessThan(swipe.input.y1 - 100);
  // ... and it is drawn on the picture, in the page's accent colour, where it went.
  const pixel = await screen.evaluate(
    (el: HTMLCanvasElement, p) => [
      ...el.getContext("2d")!.getImageData(p.x, p.y, 1, 1).data,
    ],
    middle,
  );
  expect(pixel[2]).toBeGreaterThan(pixel[0]); // bluer than the grey screen
  expect(pixel[2]).toBeGreaterThan(150);
});

test("in a takeover, a slow drag is sent against the newest picture, not the one it began on", async ({
  page,
}) => {
  await mockApi(page, []);
  const { sent, screen, push } = await takeoverWithPictures(page);
  const box = (await screen.boundingBox())!;
  const x0 = box.x + box.width * 0.2;
  const y0 = box.y + box.height * 0.5;
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  // While the finger is down, 25 more pictures of the same screen arrive.
  push(
    Array.from({ length: 25 }, (_, i) => ({
      frame_id: i + 2,
      width: 1080,
      height: 2400,
    })),
  );
  for (let i = 1; i <= 12; i += 1) {
    await page.mouse.move(x0 + (i / 12) * box.width * 0.6, y0);
    await page.waitForTimeout(40);
  }
  await page.mouse.up();
  await expect
    .poll(() => sent.filter((m) => m.type === "input"))
    .toHaveLength(1);
  const swipe = sent.find((m) => m.type === "input") as {
    frame_id: number;
    input: { kind: string; x1: number; x2: number; y1: number };
  };
  expect(swipe.frame_id).toBe(26);
  expect(swipe.input.kind).toBe("swipe");
  // The way is still where the finger went, in the pixels of the (unchanged) screen:
  // across the middle, from a fifth of the way in to four fifths (give or take the frame).
  expect(Math.abs(swipe.input.x1 - 216)).toBeLessThanOrEqual(40);
  expect(Math.abs(swipe.input.x2 - 864)).toBeLessThanOrEqual(40);
  expect(Math.abs(swipe.input.y1 - 1200)).toBeLessThanOrEqual(12);
  await expect(page.getByText(/^Not done/)).toHaveCount(0);
});

test("in a takeover, a drag during which the phone turned is not sent, and says so", async ({
  page,
}) => {
  await mockApi(page, []);
  const { sent, screen, push } = await takeoverWithPictures(page);
  const box = (await screen.boundingBox())!;
  const x0 = box.x + box.width * 0.2;
  const y0 = box.y + box.height * 0.5;
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  await page.mouse.move(x0 + box.width * 0.3, y0);
  await page.waitForTimeout(40);
  push([{ frame_id: 2, width: 2400, height: 1080 }]); // the phone is on its side now
  await expect(screen).toHaveJSProperty("width", 2400);
  await page.mouse.move(x0 + box.width * 0.6, y0);
  await page.waitForTimeout(40);
  await page.mouse.up();
  await expect(
    page.getByRole("status").filter({
      hasText: "Not done: the screen turned while you dragged; drag again",
    }),
  ).toBeVisible();
  expect(sent.filter((m) => m.type === "input")).toHaveLength(0);
});

/** A takeover of a phone with side keys, whose mock server answers every input as
 * carried out and can end the takeover when the test says so. */
async function takeoverWithKeys(
  page: import("@playwright/test").Page,
  { fromPool = false } = {},
) {
  await page.route("**/api/v1/devices", (route) =>
    route.fulfill({
      json: [
        {
          id: "d1",
          name: "Pixel 8",
          platform: "android",
          online: true,
          capabilities: [
            "tap",
            "back",
            "home",
            "volume_up",
            "volume_down",
            "lock",
          ],
          task_id: "t1",
          pool: "Desk Mac",
          problems: [],
          enabled: true,
        },
      ],
    }),
  );
  const sent: Record<string, unknown>[] = [];
  let end: (reason: string) => void = () => {};
  await page.routeWebSocket("**/api/v1/takeovers/k1/connect", (ws) => {
    end = (reason) => ws.send(JSON.stringify({ type: "ended", reason }));
    ws.send(
      JSON.stringify({
        type: "frame",
        frame_id: 1,
        width: 1080,
        height: 2400,
        rotation: 0,
        jpeg: SCREEN_JPEG,
      }),
    );
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
  if (fromPool) {
    // What "Take over" on a phone in the device pool opens (the sidebar is hidden on
    // a phone).
    await page.goto("/tasks/t1?takeover=d1");
  } else {
    await openTask(page);
    await page.getByRole("button", { name: "Take over" }).click();
  }
  const inputs = () =>
    sent
      .filter((m) => m.type === "input")
      .map((m) => (m.input as { kind: string }).kind);
  return { sent, inputs, end: (reason: string) => end(reason) };
}

test("in a takeover, the phone's side keys are on its edge: volume runs at once, locking asks first and ends the takeover", async ({
  page,
}) => {
  await mockApi(page, []);
  const { inputs, end } = await takeoverWithKeys(page);
  const panel = page.getByRole("region", { name: "Controlling Pixel 8" });
  await expect(
    panel.getByRole("img", { name: "Live screen of Pixel 8" }),
  ).toBeVisible();

  await panel.getByRole("button", { name: "Volume up" }).click();
  await panel.getByRole("button", { name: "Volume down" }).click();
  await expect.poll(inputs).toEqual(["volume_up", "volume_down"]);

  await panel.getByRole("button", { name: "Lock" }).click();
  const question = panel.getByRole("alertdialog", { name: "Lock the phone?" });
  await expect(question).toBeVisible();
  await question.getByRole("button", { name: "Cancel" }).click();
  await expect(question).toBeHidden();
  expect(inputs()).toEqual(["volume_up", "volume_down"]); // nothing was locked

  await panel.getByRole("button", { name: "Lock" }).first().click();
  await question.getByRole("button", { name: "Lock" }).click();
  await expect.poll(inputs).toEqual(["volume_up", "volume_down", "lock"]);
  end("device_locked"); // the Gateway does not wake a phone the person locked
  await expect(panel.getByRole("alert")).toContainText(
    "You locked the phone, so the takeover ended.",
  );
});

test("in a takeover, a warning over the picture leaves the phone's side keys free", async ({
  page,
}) => {
  await mockApi(page, []);
  // A takeover about to end for want of input: the idle warning is up from the start.
  await page.route("**/api/v1/tasks/t1/takeover", (route) =>
    route.fulfill({
      status: 201,
      json: {
        takeover_id: "k1",
        device_id: "d1",
        expires_at: new Date(Date.now() + 10_000).toISOString(),
      },
    }),
  );
  await takeoverWithKeys(page);
  const panel = page.getByRole("region", { name: "Controlling Pixel 8" });
  const warning = panel
    .getByRole("status")
    .filter({ hasText: "I'm still here" });
  await expect(warning).toBeVisible();

  const box = (await warning.boundingBox())!;
  for (const name of ["Volume up", "Volume down", "Lock"]) {
    const key = (await panel
      .getByRole("button", { name })
      .first()
      .boundingBox())!;
    const apart =
      key.x + key.width <= box.x ||
      box.x + box.width <= key.x ||
      key.y + key.height <= box.y ||
      box.y + box.height <= key.y;
    expect(apart, `${name} is not under the warning`).toBe(true);
  }
  await panel.getByRole("button", { name: "Lock" }).first().click();
  await expect(
    panel.getByRole("alertdialog", { name: "Lock the phone?" }),
  ).toBeVisible();
});

test("in a takeover, the side keys answer Alt+arrow from the keyboard", async ({
  page,
}) => {
  await mockApi(page, []);
  const { inputs } = await takeoverWithKeys(page);
  const screen = page.getByRole("img", { name: "Live screen of Pixel 8" });
  await expect(screen).toBeVisible();
  await page.getByRole("region", { name: "Controlling Pixel 8" }).focus();
  await page.keyboard.press("Alt+ArrowUp");
  await page.keyboard.press("Alt+ArrowDown");
  await expect.poll(inputs).toEqual(["volume_up", "volume_down"]);
});

test("in a takeover, the recent inputs say how much was typed, never what", async ({
  page,
}) => {
  await mockApi(page, []);
  await takeoverWithKeys(page);
  const panel = page.getByRole("region", { name: "Controlling Pixel 8" });
  await panel.getByLabel("Type on the phone").fill("hunter2");
  await panel.getByRole("button", { name: "Send text" }).click();
  await panel.getByRole("button", { name: /^Recent input/ }).click();
  await expect(panel.getByRole("listitem").first()).toContainText(
    "Typed 7 characters",
  );
  await expect(panel).not.toContainText("hunter2");
});

test.describe("on a phone", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });

  test("a takeover fills the screen, with the phone's buttons at the bottom and the ending at the top", async ({
    page,
  }) => {
    await mockApi(page, []);
    const { sent, inputs } = await takeoverWithKeys(page, { fromPool: true });
    const dialog = page.getByRole("dialog", { name: "Controlling Pixel 8" });
    const screen = dialog.getByRole("img", { name: "Live screen of Pixel 8" });
    await expect(screen).toBeVisible();
    const keys = dialog.getByRole("toolbar", { name: "Phone buttons" });
    const bottom = (await keys.boundingBox())!;
    expect(844 - (bottom.y + bottom.height)).toBeLessThan(4);

    await keys.getByRole("button", { name: "Home" }).click();
    await expect.poll(inputs).toEqual(["home"]);
    await keys.getByRole("button", { name: "Type" }).click();
    await expect(dialog.getByLabel("Type on the phone")).toBeFocused();

    await dialog.getByRole("button", { name: "End", exact: true }).click();
    await page.getByRole("menuitem", { name: "Hand back and resume" }).click();
    await expect.poll(() => sent.at(-1)).toEqual({ type: "end", resume: true });
  });
});

test("in a takeover, switching the page's language keeps the takeover going", async ({
  page,
}) => {
  await mockApi(page, []);
  const { connections } = await takeoverWithPictures(page);
  expect(connections()).toBe(1);

  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitem", { name: "简体中文" }).click();

  await expect(
    page.getByRole("img", { name: "Pixel 8 的实时画面" }),
  ).toBeVisible();
  await page.waitForTimeout(300); // a socket opened again would be by now
  // The same connection: the server ends a takeover when its socket closes.
  expect(connections()).toBe(1);
});
