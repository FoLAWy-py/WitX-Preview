import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useWaitingCount } from "./title";
import type {
  DeviceView,
  ModelView,
  ScheduleView,
  TaskGroupView,
  TaskSummary,
} from "@/api/types.gen";
import { useAuth } from "@/lib/auth";
import { follow } from "@/lib/follow";
import { useAsync, type AsyncState } from "@/lib/useAsync";

/** How often the workspace loads again anyway while the page is visible: changes are
 * pushed, this is for one that ever goes without its notice. */
const FALLBACK_MS = 60_000;
/** Notices this close together are one burst, loaded once. */
const NOTICE_BURST_MS = 200;

interface Workspace {
  /** Tasks waiting for someone (an approval, an answer, an unknown effect). */
  attention: AsyncState<TaskSummary[]>;
  groups: AsyncState<TaskGroupView[]>;
  devices: AsyncState<DeviceView[]>;
  schedules: AsyncState<ScheduleView[]>;
  models: ModelView[];
  /** After the allowed providers changed: which models can be chosen. */
  reloadModels: () => Promise<void>;
  /** Counts up whenever task lists should load again. */
  tick: number;
  /** Counts up whenever devices and their pools should load again. */
  devicesTick: number;
  /** After a change to tasks or groups: every list loads again now. */
  refresh: () => void;
}

const Ctx = createContext<Workspace | null>(null);

/** Calls `fn` every `ms` while the page is visible, and at once when it becomes visible. */
export function useVisibleInterval(fn: () => void, ms: number) {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") latest.current();
    };
    const timer = setInterval(tick, ms);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [ms]);
}

/** What the sidebar and every page share: the tasks, the groups, the devices and the models. */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { api } = useAuth();
  const attention = useAsync(() => api.tasks({ attention: true }), [api]);
  useWaitingCount(attention.data?.length ?? 0);
  const groups = useAsync(() => api.taskGroups(), [api]);
  const devices = useAsync(() => api.devices(), [api]);
  const schedules = useAsync(() => api.schedules(), [api]);
  const models = useAsync(() => api.models(), [api]);
  const [tick, setTick] = useState(0);
  const [devicesTick, setDevicesTick] = useState(0);
  const refresh = useCallback(() => {
    setTick((n) => n + 1);
    void attention.reload();
    void groups.reload();
  }, [attention, groups]);
  const reloadDevices = useCallback(() => {
    setDevicesTick((n) => n + 1);
    void devices.reload();
  }, [devices]);

  // What changed is pushed: each burst of notices loads again the kinds it
  // names, and connecting (again) loads everything, since notices are not kept.
  const latest = useRef({ refresh, reloadDevices, schedules });
  latest.current = { refresh, reloadDevices, schedules };
  useEffect(() => {
    let named = new Set<string>();
    let burst: ReturnType<typeof setTimeout> | undefined;
    const load = (kinds: Set<string>) => {
      const all = kinds.has("all");
      const now = latest.current;
      if (all || kinds.has("task") || kinds.has("groups")) now.refresh();
      if (all || kinds.has("devices")) now.reloadDevices();
      if (all || kinds.has("schedules")) void now.schedules.reload();
    };
    const stop = follow(
      (after, handlers, signal) => api.workspaceEvents(after, handlers, signal),
      {
        onEvent: (event) => {
          named.add(event.type);
          clearTimeout(burst);
          burst = setTimeout(() => {
            const kinds = named;
            named = new Set();
            load(kinds);
          }, NOTICE_BURST_MS);
        },
        onConnection: (up, reconnected) => {
          if (up && reconnected) load(new Set(["all"]));
        },
      },
    );
    return () => {
      stop();
      clearTimeout(burst);
    };
  }, [api]);
  // In case a change ever goes without its notice.
  useVisibleInterval(refresh, FALLBACK_MS);
  useVisibleInterval(reloadDevices, FALLBACK_MS);
  useVisibleInterval(() => void schedules.reload(), FALLBACK_MS);
  const value = useMemo(
    () => ({
      attention,
      groups,
      devices,
      schedules,
      models: models.data ?? [],
      reloadModels: models.reload,
      tick,
      devicesTick,
      refresh,
    }),
    [attention, groups, devices, schedules, models, tick, devicesTick, refresh],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): Workspace {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useWorkspace outside WorkspaceProvider");
  return ctx;
}

/** The most tasks one request asks for; the server allows no more in a page. */
const PAGE_MAX = 500;

type TaskFilter = { group?: string; q?: string; pinned?: "only" | "without" };

/** A list's tasks as loaded so far, for which filter, and where the next page is. */
type TaskPages = { key: string; tasks: TaskSummary[]; next?: string };

/** Each task once, where it first appears: a page can overlap the one before when tasks
 * change in between. */
function once(tasks: TaskSummary[]): TaskSummary[] {
  const seen = new Set<string>();
  return tasks.filter((task) => !seen.has(task.id) && !!seen.add(task.id));
}

/**
 * One list of tasks in the sidebar (a group's, "none" for tasks in no group, or those
 * matching a search). It starts with `pageSize` tasks; `showMore` adds the next page
 * from where the list ends. With the workspace it loads again the tasks it already shows
 * (as many as the person has scrolled to), so a task that changed, moved or was deleted
 * shows that everywhere in the list. Loads run one after another, so a page that arrives
 * late never undoes a newer one.
 */
export function useTaskList(filter: TaskFilter, pageSize: number) {
  const { api } = useAuth();
  const { tick } = useWorkspace();
  const { group, q, pinned } = filter;
  const key = JSON.stringify([group, q, pinned]);
  const [list, setList] = useState<TaskPages>();
  const [error, setError] = useState<Error>();
  const [moreFailed, setMoreFailed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // What the queued loads read: the filter shown now, and the list as last loaded (set
  // at once, not on the next render, so the load after sees it).
  const keyNow = useRef(key);
  keyNow.current = key;
  const latest = useRef<TaskPages>(undefined);
  const commit = useCallback((pages: TaskPages) => {
    latest.current = pages;
    setList(pages);
  }, []);
  const shown = useRef(pageSize);
  const queue = useRef<Promise<void>>(Promise.resolve());

  const enqueue = useCallback((job: () => Promise<void>) => {
    queue.current = queue.current.then(job);
    return queue.current;
  }, []);

  // A reload that has not started yet covers any asked for meanwhile.
  const waiting = useRef<{ key: string; done: Promise<void> }>(undefined);

  const reload = useCallback(() => {
    if (waiting.current?.key === key) return waiting.current.done;
    const done = enqueue(async () => {
      waiting.current = undefined;
      const limit = Math.min(shown.current, PAGE_MAX);
      try {
        let page = await api.taskPage({ group, q, pinned, limit });
        const tasks = [...page.tasks];
        while (page.next && tasks.length < shown.current) {
          page = await api.taskPage({}, page.next);
          tasks.push(...page.tasks);
        }
        if (keyNow.current !== key) return;
        commit({ key, tasks: once(tasks), next: page.next });
        setError(undefined);
      } catch (err) {
        if (keyNow.current === key) setError(err as Error);
      }
    });
    waiting.current = { key, done };
    return done;
  }, [api, enqueue, commit, group, q, pinned, key]);

  const showMore = useCallback(() => {
    if (!latest.current?.next || loadingMore) return;
    setLoadingMore(true);
    setMoreFailed(false);
    void enqueue(async () => {
      try {
        // A reload in the meantime may have ended the list elsewhere: go on from there.
        const before = latest.current;
        if (before?.key !== key || !before.next) return;
        const page = await api.taskPage({}, before.next);
        const now = latest.current;
        if (keyNow.current !== key || now?.key !== key) return;
        shown.current += page.tasks.length;
        commit({
          key,
          tasks: once([...now.tasks, ...page.tasks]),
          next: page.next,
        });
      } catch {
        if (keyNow.current === key) setMoreFailed(true);
      } finally {
        setLoadingMore(false);
      }
    });
  }, [api, enqueue, commit, key, loadingMore]);

  // A new filter starts again from its first page.
  useEffect(() => {
    shown.current = pageSize;
    void reload();
  }, [reload, pageSize]);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    void reload();
  }, [tick, reload]);

  const mine = list?.key === key ? list : undefined;
  return {
    data: mine?.tasks,
    error: mine ? undefined : error,
    more: !!mine?.next,
    loadingMore,
    moreFailed,
    showMore,
  };
}
