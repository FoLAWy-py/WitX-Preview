import { describe, expect, it, vi } from "vitest";
import { Api, ApiError } from "./client";

function respond(status: number, body: unknown): typeof fetch {
  return vi.fn(
    async () => new Response(JSON.stringify(body), { status }),
  ) as typeof fetch;
}

describe("Api", () => {
  it("sends the session cookie and the tenant, and the CSRF token on writes", async () => {
    vi.stubGlobal("document", { cookie: "theme=dark; witx_csrf=c5rf" });
    const fetchImpl = respond(200, []);
    const api = new Api(() => ({ tenantId: "ten" }), undefined, fetchImpl);
    await api.tasks();
    await api.stop("t1");
    const calls = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls;
    const [read, write] = calls.map(([, init]) => init as RequestInit);
    expect(read.credentials).toBe("include");
    expect(read.headers).toMatchObject({ "X-Witx-Tenant": "ten" });
    expect(read.headers).not.toHaveProperty("X-CSRF-Token");
    expect(read.headers).not.toHaveProperty("Authorization");
    expect(write.headers).toMatchObject({ "X-CSRF-Token": "c5rf" });
    vi.unstubAllGlobals();
  });

  it("turns rule rejections into readable errors", async () => {
    const api = new Api(
      () => null,
      undefined,
      respond(409, { code: "task_stopping", detail: "" }),
    );
    await expect(api.stop("x")).rejects.toMatchObject({
      status: 409,
      code: "task_stopping",
    });
    await expect(api.stop("x")).rejects.toBeInstanceOf(ApiError);
  });

  it("resends a write under the same Idempotency-Key after a failure in transit", async () => {
    vi.useFakeTimers();
    const fetchImpl = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "t1" }), { status: 201 }),
      );
    const api = new Api(() => null, undefined, fetchImpl);
    const created = api.createTask({ goal: "g", device_ids: [] } as never);
    await vi.runAllTimersAsync();
    await expect(created).resolves.toMatchObject({ id: "t1" });
    vi.useRealTimers();
    const keys = fetchImpl.mock.calls.map(
      ([, init]) => (init.headers as Record<string, string>)["Idempotency-Key"],
    );
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("does not resend a write the server refused", async () => {
    const fetchImpl = respond(409, { code: "task_stopping", detail: "" });
    const api = new Api(() => null, undefined, fetchImpl);
    await expect(api.stop("x")).rejects.toBeInstanceOf(ApiError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("signs out on 401", async () => {
    const onUnauthorized = vi.fn();
    const api = new Api(() => null, onUnauthorized, respond(401, {}));
    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});
