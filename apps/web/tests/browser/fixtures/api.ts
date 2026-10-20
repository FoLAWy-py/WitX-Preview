import { type Page } from "@playwright/test";
import { PNG } from "./media";
import { task } from "./task";

export async function mockApi(
  page: Page,
  decisions: unknown[],
  modes: unknown[] = [],
  allowFull = false,
) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/methods")
      return route.fulfill({ json: { oidc: false, dev: true } });
    if (path === "/api/v1/auth/dev-login") {
      const { token } = request.postDataJSON() as { token: string };
      if (token !== "dev-token")
        return route.fulfill({ status: 401, json: {} });
      // What the server does: an HttpOnly session cookie and a readable CSRF token.
      await page.context().addCookies([
        { name: "witx_session", value: "s1", url: url.origin, httpOnly: true },
        { name: "witx_csrf", value: "c1", url: url.origin },
      ]);
      return route.fulfill({ status: 204 });
    }
    const headers = await request.allHeaders();
    if (!(headers.cookie ?? "").includes("witx_session=s1"))
      return route.fulfill({ status: 401, json: {} });
    if (request.method() !== "GET" && headers["x-csrf-token"] !== "c1")
      return route.fulfill({ status: 403, json: { detail: "csrf" } });
    if (path === "/api/v1/me")
      return route.fulfill({
        json: {
          user_id: "u",
          display_name: "Alice",
          tenant_id: "x",
          tenant_name: "Acme",
          role: "owner",
          allow_full_access: allowFull,
          fast_risk_check: false,
          allowed_providers: ["openai", "deepinfra"],
          model_fallback: false,
          screenshot_retention_days: 30,
          screenshot_retention_max: 30,
          audit_retention_days: 90,
        },
      });
    if (path === "/api/v1/models")
      return route.fulfill({
        json: [
          {
            name: "gpt-6-luna",
            provider: "openai",
            default: true,
            allowed: true,
          },
        ],
      });
    if (path === "/api/v1/devices")
      return route.fulfill({
        json: [
          {
            id: "d1",
            name: "Pixel 8",
            platform: "android",
            online: true,
            capabilities: ["tap"],
            task_id: "t1",
            pool: "Desk Mac",
            problems: [],
            enabled: true,
          },
        ],
      });
    if (path === "/api/v1/tasks")
      return route.fulfill({
        json: [
          {
            id: "t1",
            request: task.request,
            goal: task.goal,
            status: "active",
            permission_mode: "auto",
            needs_attention: true,
            created_at: task.created_at,
            updated_at: task.updated_at,
          },
        ],
      });
    if (path === "/api/v1/tasks/t1") return route.fulfill({ json: task });
    if (path === "/api/v1/tasks/t1/events")
      return route.fulfill({
        contentType: "text/event-stream",
        body: ": keep-alive\n\n",
      });
    if (path === "/api/v1/observations/o1/screenshot")
      return route.fulfill({ contentType: "image/png", body: PNG });
    if (path === "/api/v1/marketplaces")
      return route.fulfill({ json: { marketplaces: [], installed: [] } });
    if (path === "/api/v1/tasks/t1/pause")
      return route.fulfill({ json: { ...task, status: "paused" } });
    if (path === "/api/v1/tasks/t1/takeover")
      return route.fulfill({
        status: 201,
        json: {
          takeover_id: "k1",
          device_id: "d1",
          // Two minutes from now, as the server says: a time already past would put
          // the idle warning up at once, over the phone's side keys.
          expires_at: new Date(Date.now() + 120_000).toISOString(),
        },
      });
    if (path === "/api/v1/tasks/t1/permission-mode") {
      const body = route.request().postDataJSON() as {
        permission_mode: string;
      };
      modes.push(body);
      return route.fulfill({ json: { ...task, ...body } });
    }
    if (path === "/api/v1/actions/a1/approval") {
      decisions.push(route.request().postDataJSON());
      return route.fulfill({ json: { ...task.actions[0], status: "queued" } });
    }
    return route.fulfill({ status: 404, json: { code: "not_found" } });
  });
}
