import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./fixtures/api";
import { openTask } from "./fixtures/auth";
import { task } from "./fixtures/task";

const MINE = {
  name: "my:clear-list",
  plugin: "my",
  marketplace: "local",
  version: "0.1.1",
  description: "Clear a shopping list.",
  apps: ["com.example.list"],
  body: "Open the list with `launch_app`.\nTap Clear.",
  editable: true,
};

const THEIRS = {
  name: "basics:test-records",
  plugin: "basics",
  marketplace: "witx-preview",
  version: "1.1",
  description: "Create and verify a test record.",
  apps: [],
  body: "Use the exact name from the goal.",
  editable: false,
};

async function signIn(page: Page, path = "/customize") {
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/sign-in"));
  await page.goto(path);
}

test("the sidebar leads to Customize, with skills and plugins on tabs of their own; settings point there", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await signIn(page, "/");

  await page.getByRole("link", { name: "Customize" }).click();
  await expect(page).toHaveURL(/\/customize$/);
  await expect(page.getByRole("tab", { name: "Skills" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("button", { name: /my:clear-list/ }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Plugins" }).click();
  await expect(page).toHaveURL(/\/customize\?tab=plugins$/);
  await expect(page.getByText("No plugins installed yet.")).toBeVisible();

  await page.goto("/settings");
  await page.getByRole("link", { name: "Open Customize" }).click();
  await expect(page).toHaveURL(/\/customize$/);
});

test("skills are found by name, description or app, and one is read beside the list", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [THEIRS, MINE], not_loaded: [] } }),
  );
  await signIn(page);

  await expect(page.getByText("Choose a skill to read it.")).toBeVisible();
  const search = page.getByPlaceholder("Search skills");
  await search.fill("com.example");
  await expect(
    page.getByRole("button", { name: /basics:test-records/ }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await expect(page).toHaveURL(/\?skill=my%3Aclear-list$/);
  await expect(
    page.getByRole("heading", { name: "my:clear-list" }),
  ).toBeVisible();
  await expect(page.getByText("Tap Clear.")).toBeVisible();
  await search.fill("nothing like it");
  await expect(page.getByText("No skill matches.")).toBeVisible();
});

test("on a narrow screen a skill is read in place of the list, and Back returns to it", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await signIn(page);

  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await expect(
    page.getByRole("heading", { name: "my:clear-list" }),
  ).toBeVisible();
  await expect(page.getByPlaceholder("Search skills")).toBeHidden();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(page.getByPlaceholder("Search skills")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "my:clear-list" }),
  ).toHaveCount(0);
});

test("the skills the agent can load are listed, with the plugins that are not loaded and why", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({
      json: {
        skills: [THEIRS, MINE],
        not_loaded: [
          {
            plugin: "shop",
            reason: "shop:pay: no tool `teleport` in this WitX",
          },
        ],
      },
    }),
  );
  await signIn(page);

  await expect(page.getByText("Yours · v0.1.1")).toBeVisible();
  await expect(page.getByText("basics from witx-preview · v1.1")).toBeVisible();
  await expect(page.getByText("shop is not loaded:")).toBeVisible();
  await page.getByRole("button", { name: /basics:test-records/ }).click();
  await expect(
    page.getByText("Use the exact name from the goal."),
  ).toBeVisible();
  // Only the person's own skills can be changed.
  await expect(page.getByRole("button", { name: "Edit" })).toHaveCount(0);
});

test("a new skill is saved only after the person has reviewed its text", async ({
  page,
}) => {
  await mockApi(page, []);
  const saved: unknown[] = [];
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [], not_loaded: [] } }),
  );
  await page.route("**/api/v1/skills/my/send-voice", (route) => {
    saved.push(route.request().postDataJSON());
    return route.fulfill({
      json: { ...MINE, name: "my:send-voice", version: "0.1.0" },
    });
  });
  await signIn(page);

  await page.getByRole("button", { name: "New skill" }).click();
  const editor = page.getByRole("dialog", { name: "New skill" });
  await editor.getByLabel("Name", { exact: true }).fill("Send Voice");
  await expect(editor.getByText("Use lowercase letters")).toBeVisible();
  await expect(editor.getByRole("button", { name: "Review" })).toBeDisabled();
  await editor.getByLabel("Name", { exact: true }).fill("send-voice");
  await editor.getByLabel("When to use it").fill("Send a voice message.");
  await editor.getByLabel("Apps").fill("com.tencent.mm, ");
  await editor.getByLabel("How to do it").fill("Hold the mic button.");
  await editor.getByRole("button", { name: "Review" }).click();

  await expect(editor.getByLabel("Changes")).toContainText(
    "+ Hold the mic button.",
  );
  expect(saved).toEqual([]);
  await editor.getByRole("button", { name: "Save and use" }).click();
  await expect
    .poll(() => saved)
    .toEqual([
      {
        description: "Send a voice message.",
        apps: ["com.tencent.mm"],
        body: "Hold the mic button.",
        invoke: "auto",
      },
    ]);
  await expect(editor).toHaveCount(0);
});

test("an older version is restored by saving it again, after seeing what changes back", async ({
  page,
}) => {
  await mockApi(page, []);
  const saved: unknown[] = [];
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await page.route("**/api/v1/skills/my/clear-list/history", (route) =>
    route.fulfill({
      json: [
        { ...MINE, commit: "b", saved_at: "2026-10-20T10:00:00Z" },
        {
          ...MINE,
          commit: "a",
          version: "0.1.0",
          saved_at: "2026-10-19T10:00:00Z",
          body: "Open the list with `launch_app`.\nTap Delete all.",
        },
      ],
    }),
  );
  await page.route("**/api/v1/skills/my/clear-list", (route) => {
    saved.push(route.request().postDataJSON());
    return route.fulfill({ json: MINE });
  });
  await signIn(page);

  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await page.getByRole("button", { name: "Versions" }).click();
  await page
    .getByRole("dialog", { name: "Versions of my:clear-list" })
    .getByRole("button", { name: "Restore" })
    .click();
  const editor = page.getByRole("dialog", { name: "Edit my:clear-list" });
  const changes = editor.getByLabel("Changes");
  await expect(changes).toContainText("- Tap Clear.");
  await expect(changes).toContainText("+ Tap Delete all.");
  await editor.getByRole("button", { name: "Save and use" }).click();
  await expect
    .poll(() => saved)
    .toEqual([
      {
        description: MINE.description,
        apps: MINE.apps,
        body: "Open the list with `launch_app`.\nTap Delete all.",
        invoke: "auto",
      },
    ]);
});

test("a skill the server refuses stays open with the reason", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await page.route("**/api/v1/skills/my/clear-list", (route) =>
    route.fulfill({
      status: 422,
      json: {
        code: "skill_not_allowed",
        detail: "a skill never holds passwords, codes or payment details",
      },
    }),
  );
  await signIn(page);

  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await page.getByRole("button", { name: "Edit" }).click();
  const editor = page.getByRole("dialog", { name: "Edit my:clear-list" });
  await editor.getByLabel("How to do it").fill("The PIN is 4471.");
  await editor.getByRole("button", { name: "Review" }).click();
  await editor.getByRole("button", { name: "Save and use" }).click();
  await expect(editor.getByRole("alert")).toHaveText(
    "a skill never holds passwords, codes or payment details",
  );
});

async function drafted(page: Page, draft: Record<string, unknown>) {
  await page.route("**/api/v1/tasks/t1/events*", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: `data: ${JSON.stringify({
        seq: 1,
        type: "skill.drafted",
        data: draft,
        created_at: "2026-10-20T10:00:06Z",
      })}\n\n`,
    }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);
}

test("a skill the agent drafted is saved only after the person has reviewed it", async ({
  page,
}) => {
  await mockApi(page, []);
  let skills: unknown[] = [];
  const saved: unknown[] = [];
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills, not_loaded: [] } }),
  );
  await page.route("**/api/v1/skills/my/open-wifi", (route) => {
    saved.push(route.request().postDataJSON());
    skills = [{ ...MINE, name: "my:open-wifi", version: "0.1.0" }];
    return route.fulfill({ json: skills[0] });
  });
  await drafted(page, {
    name: "my:open-wifi",
    description: "Open the Wi-Fi settings.",
    apps: ["com.android.settings"],
    body: "Open Settings with `launch_app`.",
  });

  const card = page.getByRole("region", { name: "Skill draft · my:open-wifi" });
  await expect(card.getByText("Open the Wi-Fi settings.")).toBeVisible();
  await card.getByRole("button", { name: "Review and save" }).click();
  const editor = page.getByRole("dialog", { name: "New skill" });
  await expect(editor.getByLabel("Changes")).toContainText(
    "+ Open Settings with `launch_app`.",
  );
  expect(saved).toEqual([]);
  await editor.getByRole("button", { name: "Save and use" }).click();
  await expect
    .poll(() => saved)
    .toEqual([
      {
        description: "Open the Wi-Fi settings.",
        apps: ["com.android.settings"],
        body: "Open Settings with `launch_app`.",
        invoke: "auto",
      },
    ]);
  await expect(card.getByText("Saved as my:open-wifi · v0.1.0")).toBeVisible();
});

test("a draft of one of the person's skills shows what it changes before it replaces it", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await drafted(page, {
    name: MINE.name,
    description: MINE.description,
    apps: MINE.apps,
    body: "Open the list with `launch_app`.\nTap Delete all.",
  });

  const card = page.getByRole("region", {
    name: "Skill draft · my:clear-list",
  });
  await expect(
    card.getByText("Saving it replaces your my:clear-list."),
  ).toBeVisible();
  await card.getByRole("button", { name: "Review and save" }).click();
  const changes = page
    .getByRole("dialog", { name: "Edit my:clear-list" })
    .getByLabel("Changes");
  await expect(changes).toContainText("- Tap Clear.");
  await expect(changes).toContainText("+ Tap Delete all.");
});

test("a draft is tried in a new plan-mode task on the same phones, before it is saved", async ({
  page,
}) => {
  await mockApi(page, []);
  const created: Record<string, unknown>[] = [];
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [], not_loaded: [] } }),
  );
  await page.route("**/api/v1/tasks", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    created.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...task, id: "t2" } });
  });
  await drafted(page, {
    name: "my:open-wifi",
    description: "Open the Wi-Fi settings.",
    apps: ["com.android.settings"],
    body: "Open Settings with `launch_app`.",
  });

  await page
    .getByRole("region", { name: "Skill draft · my:open-wifi" })
    .getByRole("button", { name: "Try it (plan mode)" })
    .click();
  const trial = page.getByRole("dialog", { name: "Try my:open-wifi" });
  await trial
    .getByLabel("What to ask")
    .fill("Open the Wi-Fi settings of this phone");
  await trial.getByRole("button", { name: "Start the trial" }).click();
  await expect(page).toHaveURL(/\/tasks\/t2$/);
  expect(created).toHaveLength(1);
  const goal = String(created[0].goal);
  expect(goal).toMatch(
    /^\/plan Try out the skill draft my:open-wifi on this: Open the Wi-Fi settings of this phone\n/,
  );
  expect(goal.endsWith("\n\nOpen Settings with `launch_app`.")).toBe(true);
  expect(created[0]).toMatchObject({
    device_ids: task.device_ids,
    permission_mode: task.permission_mode,
  });
});

test("typing $ in a message offers the skills, and picking one names it", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [THEIRS, MINE], not_loaded: [] } }),
  );
  await page.goto("/sign-in");
  await page.getByLabel("Developer token").fill("dev-token");
  await page.getByRole("button", { name: "Sign in with token" }).click();
  await openTask(page);

  const box = page.locator("#note");
  await box.fill("清空用$cle");
  const offered = page.getByRole("listbox", { name: "$" });
  await expect(offered.getByRole("option")).toHaveText([/\$my:clear-list/]);
  await box.press("Tab");
  await expect(box).toHaveValue("清空用$my:clear-list ");
});

test("one of the person's skills can be kept for when they name it", async ({
  page,
}) => {
  await mockApi(page, []);
  const saved: unknown[] = [];
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await page.route("**/api/v1/skills/my/clear-list", (route) => {
    saved.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...MINE, invoke: "user" } });
  });
  await signIn(page);

  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await page.getByRole("button", { name: "Edit" }).click();
  const editor = page.getByRole("dialog", { name: "Edit my:clear-list" });
  await editor.getByLabel("Only when I name it").check();
  await expect(editor.getByText("$my:clear-list in a message")).toBeVisible();
  await editor.getByRole("button", { name: "Review" }).click();
  await expect(editor.getByText("Only when I name it")).toBeVisible();
  await editor.getByRole("button", { name: "Save and use" }).click();
  await expect
    .poll(() => saved)
    .toEqual([
      {
        description: MINE.description,
        apps: MINE.apps,
        body: MINE.body,
        invoke: "user",
      },
    ]);
});

test("an app's notes are turned into a skill by a task that drafts it from them only", async ({
  page,
}) => {
  await mockApi(page, []);
  const created: Record<string, unknown>[] = [];
  const note = (id: string, app: string, text: string) => ({
    id,
    text,
    kind: "app",
    app,
    by_agent: true,
    source_task_id: null,
    created_at: "2026-10-20T10:00:00Z",
  });
  await page.route("**/api/v1/memories", (route) =>
    route.fulfill({
      json: [
        note("m1", "com.apple.Preferences", "Apps is below General."),
        note("m2", "com.apple.Preferences", "The S index jumps to Safari."),
        note("m3", "com.apple.Preferences", "Search often finds nothing."),
        note("m4", "com.apple.reminders", "The + is at the bottom right."),
      ],
    }),
  );
  await page.route("**/api/v1/tasks", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    created.push(route.request().postDataJSON());
    return route.fulfill({ json: { ...task, id: "t2" } });
  });
  await signIn(page, "/settings");

  await expect(
    page.getByText("3 notes about com.apple.Preferences"),
  ).toBeVisible();
  // One note is not yet a way of doing something.
  await expect(page.getByText(/notes about com.apple.reminders/)).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Turn into a skill" }).click();
  await expect(page).toHaveURL(/\/tasks\/t2$/);
  expect(created[0].device_ids).toEqual([]);
  expect(String(created[0].goal)).toMatch(
    /^\/skill Turn what is kept about the app com\.apple\.Preferences into one skill\..*\n- Apps is below General\.\n- The S index jumps to Safari\.\n- Search often finds nothing\.$/s,
  );
});

const OFFER = {
  name: "basics",
  version: "1.2.0",
  description: "Basic know-how.",
  skills: ["basics:test-records"],
  source: "https://github.com/FoLAWy-py/witx-plugins-preview.git",
  commit: "a".repeat(40),
  sha256: "b".repeat(64),
  trouble: [],
  files: [
    {
      path: "skills/test-records/SKILL.md",
      text: "Use the exact name.\nThen check it.",
      installed: null,
    },
  ],
  removed: [],
};

async function marketplaces(
  page: Page,
  installed: unknown[],
  offers: unknown[],
) {
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [], not_loaded: [] } }),
  );
  await page.route("**/api/v1/marketplaces", (route) =>
    route.fulfill({
      json: {
        marketplaces: [{ name: "witx-preview", source: OFFER.source }],
        installed,
      },
    }),
  );
  await page.route("**/api/v1/marketplaces/witx-preview/plugins", (route) =>
    route.fulfill({ json: offers }),
  );
}

test("a plugin is installed once the person has read it, bound to what they read", async ({
  page,
}) => {
  await mockApi(page, []);
  const installs: unknown[] = [];
  await marketplaces(page, [], [OFFER]);
  await page.route(
    "**/api/v1/marketplaces/witx-preview/plugins/basics/install",
    (route) => {
      installs.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          name: "basics",
          marketplace: "witx-preview",
          version: "1.2.0",
          commit: OFFER.commit,
        },
      });
    },
  );
  await signIn(page, "/customize?tab=plugins");

  await page.getByRole("button", { name: "Look" }).click();
  await page.getByRole("button", { name: "Install" }).click();
  const read = page.getByRole("dialog", { name: "Install basics v1.2.0" });
  await expect(read.getByLabel("skills/test-records/SKILL.md")).toHaveText(
    "Use the exact name.Then check it.",
  );
  await expect(
    read.getByText(/runs no code and grants no permission/),
  ).toBeVisible();
  expect(installs).toEqual([]);
  await read.getByRole("button", { name: "Install" }).click();
  await expect
    .poll(() => installs)
    .toEqual([{ commit: OFFER.commit, sha256: OFFER.sha256 }]);
  await expect(read).toHaveCount(0);
});

test("an update shows what changes, and a plugin that does not fit cannot be installed", async ({
  page,
}) => {
  await mockApi(page, []);
  const installed = {
    name: "basics",
    marketplace: "witx-preview",
    version: "1.1.0",
    commit: "c".repeat(40),
  };
  const update = {
    ...OFFER,
    files: [
      { ...OFFER.files[0], installed: "Use the exact name.\nThen look." },
    ],
  };
  const unfit = {
    ...OFFER,
    name: "shop",
    trouble: ["shop:pay: no tool `teleport` in this WitX"],
  };
  await marketplaces(page, [installed], [update, unfit]);
  await signIn(page, "/customize?tab=plugins");

  await expect(page.getByText("v1.1.0 from witx-preview")).toBeVisible();
  await page.getByRole("button", { name: "Look" }).click();
  await page.getByRole("button", { name: "Update" }).click();
  const changes = page
    .getByRole("dialog", { name: "Update basics to v1.2.0" })
    .getByLabel("skills/test-records/SKILL.md");
  await expect(changes).toContainText("- Then look.");
  await expect(changes).toContainText("+ Then check it.");
  await page.getByRole("button", { name: "Cancel" }).click();

  await page.getByRole("button", { name: "Install" }).click();
  const refused = page.getByRole("dialog", { name: "Install shop v1.2.0" });
  await expect(refused.getByRole("alert")).toHaveText(
    "shop:pay: no tool `teleport` in this WitX",
  );
  await expect(refused.getByRole("button", { name: "Install" })).toBeDisabled();
});

test("an installed plugin shows the skills it brings, and one opens on the Skills tab", async ({
  page,
}) => {
  await mockApi(page, []);
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [THEIRS, MINE], not_loaded: [] } }),
  );
  await page.route("**/api/v1/marketplaces", (route) =>
    route.fulfill({
      json: {
        marketplaces: [],
        installed: [
          {
            name: "basics",
            marketplace: "witx-preview",
            version: "1.1",
            commit: "c".repeat(40),
          },
        ],
      },
    }),
  );
  await signIn(page, "/customize?tab=plugins");

  await expect(page.getByText("cccccccccccc")).toBeVisible();
  await page.getByRole("button", { name: "basics:test-records" }).click();
  await expect(page).toHaveURL(/\/customize\?skill=basics%3Atest-records$/);
  await expect(
    page.getByRole("heading", { name: "basics:test-records" }),
  ).toBeVisible();
});

test("a skill is deleted with its versions kept, unless the person also deletes every version", async ({
  page,
}) => {
  await mockApi(page, []);
  const deleted: string[] = [];
  await page.route("**/api/v1/skills", (route) =>
    route.fulfill({ json: { skills: [MINE], not_loaded: [] } }),
  );
  await page.route("**/api/v1/skills/my/clear-list*", (route) => {
    deleted.push(new URL(route.request().url()).search);
    return route.fulfill({ status: 204 });
  });
  await signIn(page);

  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await page.getByRole("button", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete my:clear-list?" });
  await expect(dialog.getByText("Its versions are kept")).toBeVisible();
  await dialog.getByLabel("Also delete every saved version").check();
  await expect(
    dialog.getByText("all its saved versions are erased"),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Delete for good" }).click();
  await expect.poll(() => deleted).toEqual(["?purge=true"]);
  await expect(dialog).toHaveCount(0);

  await page.getByRole("button", { name: /my:clear-list/ }).click();
  await page.getByRole("button", { name: "Delete" }).click();
  await page
    .getByRole("dialog", { name: "Delete my:clear-list?" })
    .getByRole("button", { name: "Delete" })
    .click();
  await expect.poll(() => deleted).toEqual(["?purge=true", ""]);
});
