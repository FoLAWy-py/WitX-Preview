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
import { useAsync, type AsyncState } from "@/lib/useAsync";

/** How often task lists refresh while the page is visible. */
const TASKS_MS = 5_000;

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
  const refresh = useCallback(() => {
    setTick((n) => n + 1);
    void attention.reload();
    void groups.reload();
  }, [attention, groups]);
  useVisibleInterval(refresh, TASKS_MS);
  useVisibleInterval(() => void devices.reload(), 10_000);
  useVisibleInterval(() => void schedules.reload(), 30_000);
  const value = useMemo(
    () => ({
      attention,
      groups,
      devices,
      schedules,
      models: models.data ?? [],
      reloadModels: models.reload,
      tick,
      refresh,
    }),
    [attention, groups, devices, schedules, models, tick, refresh],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): Workspace {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useWorkspace outside WorkspaceProvider");
  return ctx;
}

/**
 * One list of tasks in the sidebar (a group's, "none" for tasks in no group, or those
 * matching a search), `pageSize` at a time: `showMore` loads the next page. It reloads
 * with the workspace.
 */
export function useTaskList(
  filter: { group?: string; q?: string; pinned?: "only" | "without" },
  pageSize: number,
) {
  const { api } = useAuth();
  const { tick } = useWorkspace();
  const [limit, setLimit] = useState(pageSize);
  const { group, q, pinned } = filter;
  const list = useAsync(
    () => api.tasks({ group, q, pinned, limit }),
    [api, group, q, pinned, limit],
  );
  const { reload } = list;
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    void reload();
  }, [tick, reload]);
  return {
    ...list,
    more: (list.data?.length ?? 0) >= limit,
    showMore: () => setLimit((n) => n + pageSize),
  };
}
