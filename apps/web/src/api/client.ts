import { newId } from "@/lib/id";
import { parseSse } from "./sse";
import type {
  ReasoningEffortUpdate,
  SayingView,
  ActionView,
  AuthMethods,
  ApprovalDecision,
  DeviceChange,
  DeviceEnable,
  DeviceView,
  GatewayCreated,
  GatewayView,
  GrantView,
  MemorySettings,
  MemoryView,
  PurchaseChoice,
  PurchaseView,
  EventView,
  Me,
  ModelUpdate,
  LiveFrame,
  ModelView,
  PermissionModeUpdate,
  Reconcile,
  TaskCreate,
  ScheduleCreate,
  RetentionSetting,
  ScheduleView,
  SideChatAsk,
  SkillSave,
  MarketplacesView,
  PluginOfferView,
  PluginInstall,
  InstalledPluginView,
  SkillsView,
  SkillVersionView,
  SkillView,
  SideMessageView,
  TaskChanges,
  TaskDetail,
  TaskEarlier,
  TaskInputCreate,
  TaskGroupView,
  TaskSummary,
  TakeoverEnd,
  TakeoverStart,
  TakeoverView,
  TenantSettings,
} from "./types.gen";

const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
// A write whose request failed in transit may or may not have arrived; it is sent
// again under the same Idempotency-Key, so the server acts on it at most once.
const WRITE_RETRIES = 2;
const RETRY_DELAY_MS = 500;

export interface TaskFilter {
  group?: string;
  attention?: boolean;
  q?: string;
  limit?: number;
  /** Only the tasks the signed-in person pinned, or none of them. */
  pinned?: "only" | "without";
}

function tasksPath(filter: TaskFilter): string {
  const query = new URLSearchParams();
  if (filter.q) query.set("q", filter.q);
  if (filter.pinned) query.set("pinned", filter.pinned);
  if (filter.group) query.set("group", filter.group);
  if (filter.attention) query.set("attention", "true");
  if (filter.limit) query.set("limit", String(filter.limit));
  const qs = query.toString();
  return `/api/v1/tasks${qs ? `?${qs}` : ""}`;
}

/** The next page's path from a `Link: <…>; rel="next"` header, if more follow. */
export function nextPage(link: string | null): string | undefined {
  return link?.match(/<([^>]+)>\s*;\s*rel="next"/)?.[1];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export interface StreamHandlers {
  /** The stream is connected. */
  onOpen: () => void;
  onEvent: (event: EventView) => void;
  /** What the agent is saying right now: not an event, an empty text ends it. */
  onSaying?: (saying: SayingView) => void;
  /** Anything arrived, including a keep-alive. */
  onActivity: () => void;
}

export interface Context {
  /** Which tenant to act in, for users who belong to several. */
  tenantId?: string;
}

type Effort = ReasoningEffortUpdate["reasoning_effort"];

/** The CSRF token the server set next to the session cookie (readable on purpose). */
function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const match = /(?:^|;\s*)witx_csrf=([^;]*)/.exec(document.cookie);
  return match ? decodeURIComponent(match[1]) : undefined;
}

/**
 * Thin typed wrapper around the cloud's /api/v1. The browser is signed in by an
 * HttpOnly session cookie that this code never sees; every write echoes
 * the CSRF token.
 */
export class Api {
  constructor(
    private readonly context: () => Context | null = () => null,
    private readonly onUnauthorized: () => void = () => {},
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  private headers(
    write: boolean,
    extra: Record<string, string> = {},
  ): Record<string, string> {
    const headers: Record<string, string> = { ...extra };
    const tenantId = this.context()?.tenantId;
    if (tenantId) headers["X-Witx-Tenant"] = tenantId;
    const csrf = write ? csrfToken() : undefined;
    if (csrf) headers["X-CSRF-Token"] = csrf;
    return headers;
  }

  private async request(
    path: string,
    init: RequestInit = {},
    extraHeaders: Record<string, string> = {},
  ): Promise<Response> {
    const write = !!init.method && init.method !== "GET";
    const response = await this.fetchImpl(BASE + path, {
      ...init,
      credentials: "include",
      headers: this.headers(write, {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...extraHeaders,
      }),
    });
    if (response.ok) return response;
    if (response.status === 401) this.onUnauthorized();
    let code = `http_${response.status}`;
    let message = response.statusText || code;
    try {
      const body = (await response.json()) as {
        code?: string;
        detail?: unknown;
      };
      if (body.code) code = body.code;
      if (typeof body.detail === "string" && body.detail) message = body.detail;
      else if (body.code) message = body.code.replace(/_/g, " ");
    } catch {
      // keep the HTTP status text
    }
    throw new ApiError(response.status, code, message);
  }

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    return (await (await this.request(path, init)).json()) as T;
  }

  /** A write: one Idempotency-Key, resent after a failure in transit. */
  private async write<T>(
    method: "POST" | "PUT" | "PATCH",
    path: string,
    body: unknown,
  ): Promise<T> {
    const key = newId();
    const init = { method, body: JSON.stringify(body) };
    for (let attempt = 0; ; attempt++) {
      try {
        const response = await this.request(path, init, {
          "Idempotency-Key": key,
        });
        return (
          response.status === 204 ? undefined : await response.json()
        ) as T;
      } catch (err) {
        // The server answered: retrying would get the same answer.
        if (err instanceof ApiError || attempt >= WRITE_RETRIES) throw err;
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS * (attempt + 1)));
      }
    }
  }

  private post<T>(path: string, body: unknown): Promise<T> {
    return this.write<T>("POST", path, body);
  }

  private put<T>(path: string, body: unknown): Promise<T> {
    return this.write<T>("PUT", path, body);
  }

  me = () => this.json<Me>("/api/v1/me");
  authMethods = () => this.json<AuthMethods>("/api/v1/auth/methods");
  devLogin = (token: string) =>
    this.post<void>("/api/v1/auth/dev-login", { token });
  logout = () => this.post<void>("/api/v1/auth/logout", {});
  /** Deletes a finished task; it disappears at once. */
  deleteTask = async (taskId: string) => {
    await this.request(`/api/v1/tasks/${taskId}`, { method: "DELETE" });
  };
  /** Where the browser goes to sign in with the identity provider. */
  loginUrl = (returnTo: string) =>
    `${BASE}/api/v1/auth/login?return_to=${encodeURIComponent(returnTo)}`;
  setTenantSettings = (body: TenantSettings) =>
    this.put<TenantSettings>("/api/v1/tenant/settings", body);
  devices = () => this.json<DeviceView[]>("/api/v1/devices");
  gateways = () => this.json<GatewayView[]>("/api/v1/gateways");
  addGateway = (name: string) =>
    this.post<GatewayCreated>("/api/v1/gateways", { name });
  changeGateway = (id: string, change: "disable" | "enable" | "revoke") =>
    this.post<GatewayView>(`/api/v1/gateways/${id}/${change}`, {});
  enableDevice = (id: string, body: DeviceEnable) =>
    this.post<DeviceView>(`/api/v1/devices/${id}/enable`, body);
  disableDevice = (id: string) =>
    this.post<DeviceView>(`/api/v1/devices/${id}/disable`, {});
  deviceChanges = (id: string) =>
    this.json<DeviceChange[]>(`/api/v1/devices/${id}/changes`);
  /** Recent tasks: of one group (`group` is its ID, or "none"), or those waiting for someone. */
  pinTask = async (id: string) => {
    await this.request(`/api/v1/tasks/${id}/pin`, { method: "PUT" });
  };
  unpinTask = async (id: string) => {
    await this.request(`/api/v1/tasks/${id}/pin`, { method: "DELETE" });
  };
  tasks = (filter: TaskFilter = {}) =>
    this.json<TaskSummary[]>(tasksPath(filter));
  /** One page of a task list: the first, or the one at `next` from the page before. */
  taskPage = async (
    filter: TaskFilter,
    next?: string,
  ): Promise<{ tasks: TaskSummary[]; next?: string }> => {
    const response = await this.request(next ?? tasksPath(filter));
    const tasks = (await response.json()) as TaskSummary[];
    return { tasks, next: nextPage(response.headers.get("Link")) };
  };
  taskGroups = () => this.json<TaskGroupView[]>("/api/v1/task-groups");
  createTaskGroup = (name: string) =>
    this.post<TaskGroupView>("/api/v1/task-groups", { name });
  renameTaskGroup = (id: string, name: string) =>
    this.write<TaskGroupView>("PATCH", `/api/v1/task-groups/${id}`, { name });
  deleteTaskGroup = async (id: string) => {
    await this.request(`/api/v1/task-groups/${id}`, { method: "DELETE" });
  };
  moveTask = (taskId: string, groupId: string | null) =>
    this.put<TaskSummary>(`/api/v1/tasks/${taskId}/group`, {
      group_id: groupId,
    });
  /** The task as its page shows it: the latest part of a long one, or from
   * `from` on; with `types`, the events of those kinds in it. */
  task = (
    id: string,
    part: { from?: string; types?: Iterable<string> } = {},
  ) => {
    const query = new URLSearchParams();
    if (part.from) query.set("from", part.from);
    if (part.types) query.set("types", [...part.types].join(","));
    const qs = query.toString();
    return this.json<TaskDetail>(`/api/v1/tasks/${id}${qs ? `?${qs}` : ""}`);
  };
  /** The part of a task before `before` (its `earlier`). */
  taskEarlier = (id: string, before: string, types: Iterable<string>) =>
    this.json<TaskEarlier>(
      `/api/v1/tasks/${id}/earlier?${new URLSearchParams({
        before,
        types: [...types].join(","),
      }).toString()}`,
    );
  /** What changed in the task after the event `since`, with the records `open` (not
   * finished as the page holds them) as they are now (see `applyChanges`). */
  taskChanges = (id: string, since: number, open: string[]) =>
    this.json<TaskChanges>(
      `/api/v1/tasks/${id}/changes?since=${since}&open=${open.join(",")}`,
    );
  schedules = () => this.json<ScheduleView[]>("/api/v1/schedules");
  createSchedule = (body: ScheduleCreate) =>
    this.post<ScheduleView>("/api/v1/schedules", body);
  /** A new definition: what it does and when; it stays on or off as it was. */
  editSchedule = (id: string, body: ScheduleCreate) =>
    this.write<ScheduleView>("PUT", `/api/v1/schedules/${id}`, body);
  setScheduleEnabled = (id: string, enabled: boolean) =>
    this.write<ScheduleView>("PATCH", `/api/v1/schedules/${id}`, { enabled });
  deleteSchedule = async (id: string) => {
    await this.request(`/api/v1/schedules/${id}`, { method: "DELETE" });
  };
  /** The screen of a device the task holds, now; refused while the device is busy. */
  liveFrame = (taskId: string, deviceId: string) =>
    this.json<LiveFrame>(`/api/v1/tasks/${taskId}/devices/${deviceId}/live`);
  createTask = (body: TaskCreate) =>
    this.post<TaskDetail>("/api/v1/tasks", body);
  addInput = (id: string, body: TaskInputCreate) =>
    this.post<TaskDetail>(`/api/v1/tasks/${id}/inputs`, body);
  /** Withdraws one's own message while the main agent has not read it. */
  withdrawInput = async (id: string, seq: number) =>
    (await (
      await this.request(`/api/v1/tasks/${id}/inputs/${seq}`, {
        method: "DELETE",
      })
    ).json()) as TaskDetail;
  /** The main agent reads the queued messages now. */
  steer = (id: string) =>
    this.post<TaskDetail>(`/api/v1/tasks/${id}/steer`, {});
  pause = (id: string) =>
    this.post<TaskDetail>(`/api/v1/tasks/${id}/pause`, {});
  resume = (id: string) =>
    this.post<TaskDetail>(`/api/v1/tasks/${id}/resume`, {});
  stop = (id: string) => this.post<TaskDetail>(`/api/v1/tasks/${id}/stop`, {});
  takeover = (id: string, body: TakeoverStart) =>
    this.post<TakeoverView>(`/api/v1/tasks/${id}/takeover`, body);
  /** Side chats: questions about a task, answered without acting. */
  sideChat = (taskId: string) =>
    this.json<SideMessageView[]>(`/api/v1/tasks/${taskId}/side-chat`);
  askSideChat = (taskId: string, body: SideChatAsk) =>
    this.post<SideMessageView>(`/api/v1/tasks/${taskId}/side-chat`, body);
  closeSideChat = async (taskId: string, threadId: string) => {
    await this.request(`/api/v1/tasks/${taskId}/side-chat/${threadId}`, {
      method: "DELETE",
    });
  };
  /** Takes over a phone no task holds, from the device pool. */
  takeoverDevice = (deviceId: string) =>
    this.post<TakeoverView>(`/api/v1/devices/${deviceId}/takeover`, {});
  /** The task, or null for a phone taken over outside a task. */
  endTakeover = (takeoverId: string, body: TakeoverEnd) =>
    this.post<TaskDetail | null>(`/api/v1/takeovers/${takeoverId}/end`, body);

  /** The takeover socket. The session cookie signs it in (same site, checked Origin). */
  takeoverSocketUrl(takeoverId: string): string {
    const url = new URL(BASE || "/", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `/api/v1/takeovers/${takeoverId}/connect`;
    const tenantId = this.context()?.tenantId;
    if (tenantId) url.searchParams.set("tenant_id", tenantId);
    return url.toString();
  }
  models = () => this.json<ModelView[]>("/api/v1/models");
  setDevices = (id: string, deviceIds: string[]) =>
    this.put<TaskDetail>(`/api/v1/tasks/${id}/devices`, {
      device_ids: deviceIds,
    });
  setReasoningEffort = (id: string, effort: Effort) =>
    this.put<TaskDetail>(`/api/v1/tasks/${id}/reasoning-effort`, {
      reasoning_effort: effort,
    });
  setModel = (id: string, body: ModelUpdate) =>
    this.put<TaskDetail>(`/api/v1/tasks/${id}/model`, body);
  setPermissionMode = (id: string, body: PermissionModeUpdate) =>
    this.put<TaskDetail>(`/api/v1/tasks/${id}/permission-mode`, body);
  decide = (actionId: string, body: ApprovalDecision) =>
    this.post<ActionView>(`/api/v1/actions/${actionId}/approval`, body);
  memories = () => this.json<MemoryView[]>("/api/v1/memories");
  addMemory = (text: string) =>
    this.post<MemoryView>("/api/v1/memories", { text });
  /** Whether agents keep what they learn for you on their own. */
  setRetention = (days: number) =>
    this.write<RetentionSetting>("PUT", "/api/v1/me/retention", {
      screenshot_retention_days: days,
    });
  setAutoMemory = (auto_memory: boolean) =>
    this.put<MemorySettings>("/api/v1/memories/settings", { auto_memory });
  deleteMemory = (memoryId: string) =>
    this.request(`/api/v1/memories/${memoryId}`, { method: "DELETE" });

  /** Every installed skill; the person's own (plugin `my`) can be changed. */
  skills = () => this.json<SkillsView>("/api/v1/skills");
  saveSkill = (name: string, skill: SkillSave) =>
    this.put<SkillView>(`/api/v1/skills/my/${encodeURIComponent(name)}`, skill);
  /** With `purge`, its saved versions go too. */
  deleteSkill = (name: string, purge = false) =>
    this.request(
      `/api/v1/skills/my/${encodeURIComponent(name)}${purge ? "?purge=true" : ""}`,
      { method: "DELETE" },
    );
  skillHistory = (name: string) =>
    this.json<SkillVersionView[]>(
      `/api/v1/skills/my/${encodeURIComponent(name)}/history`,
    );
  marketplaces = () => this.json<MarketplacesView>("/api/v1/marketplaces");
  /** What a marketplace offers now, with each plugin's text (fetched: may be slow). */
  offers = (marketplace: string) =>
    this.json<PluginOfferView[]>(
      `/api/v1/marketplaces/${encodeURIComponent(marketplace)}/plugins`,
    );
  /** Installs or updates to exactly what was read: its commit and digest. */
  installPlugin = (marketplace: string, name: string, seen: PluginInstall) =>
    this.post<InstalledPluginView>(
      `/api/v1/marketplaces/${encodeURIComponent(marketplace)}/plugins/${encodeURIComponent(name)}/install`,
      seen,
    );
  removePlugin = async (name: string) => {
    await this.request(`/api/v1/plugins/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
  };
  choosePurchase = (purchaseId: string, body: PurchaseChoice) =>
    this.post<PurchaseView>(`/api/v1/purchases/${purchaseId}/choice`, body);
  revokeGrant = (grantId: string) =>
    this.post<GrantView>(`/api/v1/grants/${grantId}/revoke`, {});
  reconcile = (actionId: string, body: Reconcile) =>
    this.post<ActionView>(`/api/v1/actions/${actionId}/reconcile`, body);

  /** The screenshot, or with `width` a smaller JPEG of it for a thumbnail. */
  async screenshot(
    observationId: string,
    signal?: AbortSignal,
    width?: number,
  ): Promise<Blob> {
    const query = width ? `?width=${width}` : "";
    const response = await this.request(
      `/api/v1/observations/${observationId}/screenshot${query}`,
      { signal },
    );
    return response.blob();
  }

  /** Reads a task's event stream after `after` until it ends or `signal` aborts. */
  async events(
    taskId: string,
    after: number,
    handlers: StreamHandlers,
    signal: AbortSignal,
    /** Of the events stored before the stream opens, only these types; all later ones. */
    types?: readonly string[],
  ): Promise<void> {
    const only = types ? `&types=${types.join(",")}` : "";
    await this.stream(
      `/api/v1/tasks/${taskId}/events?after=${after}${only}`,
      handlers,
      signal,
    );
  }

  /** Reads the workspace's notices of changes until the stream ends or `signal`
   * aborts. It counts as open once the server says it listens: what is loaded after
   * that misses no change. */
  async workspaceEvents(
    _after: number,
    handlers: StreamHandlers,
    signal: AbortSignal,
  ): Promise<void> {
    await this.stream("/api/v1/workspace/events", handlers, signal, true);
  }

  private async stream(
    path: string,
    handlers: StreamHandlers,
    signal: AbortSignal,
    openOnFirstWord = false,
  ): Promise<void> {
    const response = await this.request(path, { signal });
    const reader = response.body?.getReader();
    if (!reader) return;
    if (!openOnFirstWord) handlers.onOpen();
    let opened = !openOnFirstWord;
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      if (!opened) {
        opened = true;
        handlers.onOpen();
      }
      handlers.onActivity(); // events and keep-alives alike
      buffer += decoder.decode(value, { stream: true });
      const { messages, rest } = parseSse(buffer);
      buffer = rest;
      for (const message of messages) {
        if (message.event === "agent.saying")
          handlers.onSaying?.(JSON.parse(message.data) as SayingView);
        else handlers.onEvent(JSON.parse(message.data) as EventView);
      }
    }
  }
}
