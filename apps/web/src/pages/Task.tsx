import {
  useCallback,
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import {
  MoreHorizontal,
  OctagonX,
  PanelRight,
  ShieldOff,
  Trash2,
  WifiOff,
} from "lucide-react";
import type {
  EventView,
  TakeoverView,
  TaskDetail,
  SayingView,
} from "@/api/types.gen";
import { ApiError } from "@/api/client";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { follow } from "@/lib/follow";
import { cn } from "@/lib/format";
import { useLayout } from "@/lib/prefs";
import { PANEL_MIN, usePanelWidth } from "@/lib/panelWidth";
import {
  applyChanges,
  applyEarlier,
  heldOpen,
  mergeEvents,
  OPEN_HELD_MAX,
} from "@/lib/changes";
import { useNearEnd } from "@/lib/reach";
import { buildTimeline, inPart, SHOWN_EVENTS } from "@/lib/timeline";
import { usePageTitle } from "@/lib/title";
import { useWorkspace } from "@/lib/workspace";
import { TaskComposer } from "@/components/Composer";
import { Conversation } from "@/components/Conversation";
import { loadMarkdown } from "@/components/Rich";
import { GoalBar } from "@/components/GoalTab";
import { useSideChats } from "@/components/SideChatTab";
import { SidePanel, usePanelTabs, type PanelTab } from "@/components/SidePanel";
import { PageBar } from "@/components/Shell";
import { Fade, useScrollEdges } from "@/components/ui/fade";
import { Takeover } from "@/components/Takeover";
import {
  Button,
  EmptyState,
  ErrorState,
  Ico,
  Spinner,
} from "@/components/ui/base";
import { Dialog, Sheet } from "@/components/ui/dialog";
import { Menu, type MenuEntry } from "@/components/ui/popover";
import { TaskStatusChip } from "@/components/ui/status";
import { waitsForPhone } from "@/lib/agents";

/** How long the stream may be down before the page says it may be out of date. */
const OFFLINE_NOTICE_MS = 3_000;

/** How long the agent's live words stay once its step is over: the message kept for
 * them arrives with the task fetched after the step's events, a moment later. */
const SAID_MS = 2000;

interface Live {
  /** The events the conversation shows, of one read of the stream. */
  onEvents: (events: EventView[]) => void;
  /** Something changed: a burst of events is over (`whole`: after a reconnect, when
   * some changes made meanwhile, an approval expiring say, are no events). */
  onChange: (whole: boolean) => void;
}

/**
 * Follows the task's event stream from `after` (where the task as loaded ends): passes
 * on the events the conversation shows, and says when something changed.
 */
function useEvents(taskId: string, after: number | undefined, live: Live) {
  const { api } = useAuth();
  const [saying, setSaying] = useState<SayingView | null>(null);
  const [connected, setConnected] = useState(true);
  const liveRef = useRef(live);
  liveRef.current = live;
  // Where the stream starts: the task's last event when it was first loaded. The page
  // stays mounted from one task to the next, so it is kept with the task it is of.
  const from = useRef<{ taskId: string; after: number }>(undefined);
  if (after !== undefined && from.current?.taskId !== taskId)
    from.current = { taskId, after };
  const started = after !== undefined;

  useEffect(() => {
    if (!started) return;
    let burst: ReturnType<typeof setTimeout> | undefined;
    let said: ReturnType<typeof setTimeout> | undefined;
    let offline: ReturnType<typeof setTimeout> | undefined;
    // Events of one read are passed on together: one copy of the list, not one each.
    let arriving: EventView[] = [];
    const pass = () => {
      const fresh = arriving;
      arriving = [];
      liveRef.current.onEvents(fresh);
    };
    setSaying(null);
    setConnected(true);
    const stop = follow(
      (cursor, handlers, signal) =>
        api.events(taskId, cursor, handlers, signal, [...SHOWN_EVENTS]),
      {
        onEvent: (event) => {
          if (SHOWN_EVENTS.has(event.type)) {
            if (!arriving.length) queueMicrotask(pass);
            arriving.push(event);
          }
          clearTimeout(burst);
          burst = setTimeout(() => liveRef.current.onChange(false), 150);
        },
        // What the agent says and is at right now. No words and no phase says
        // the step is over: its words stay a moment, until the message kept for them
        // has been fetched.
        onSaying: (now) => {
          clearTimeout(said);
          if (now.text || now.phase) setSaying(now);
          else said = setTimeout(() => setSaying(null), SAID_MS);
        },
        onConnection: (up, reconnected) => {
          clearTimeout(offline);
          if (!up) {
            offline = setTimeout(() => setConnected(false), OFFLINE_NOTICE_MS);
            return;
          }
          setConnected(true);
          if (reconnected) liveRef.current.onChange(true);
        },
      },
      undefined,
      from.current?.after,
    );
    return () => {
      stop();
      clearTimeout(burst);
      clearTimeout(said);
      clearTimeout(offline);
    };
  }, [api, taskId, started]);

  return { connected, saying };
}

/**
 * The task as its page shows it, with the events of its conversation. A long task
 * comes in parts: the latest when the page opens, the one before each time
 * `earlier()` asks. It is fetched whole (as far back as the page holds) after a
 * reconnect and when the page is seen again (`load(true)`), and otherwise brought up
 * to date with what changed after the last event it holds (`load(false)`), so a step
 * costs what it changed. Loads run one after another; one not started yet covers
 * those asked for meanwhile, and a whole one covers changes.
 */
function useTask(taskId: string) {
  const { api } = useAuth();
  const [state, setState] = useState<{
    taskId: string;
    task?: TaskDetail;
    events: EventView[];
    error?: Error;
  }>();
  // The task the page shows now, and the last one loaded for it, for the loads queued.
  const shownId = useRef(taskId);
  shownId.current = taskId;
  const held = useRef<{ taskId: string; task: TaskDetail }>(undefined);
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const waiting = useRef<{
    taskId: string;
    whole: boolean;
    done: Promise<void>;
  }>(undefined);

  const enqueue = useCallback(<T,>(job: () => Promise<T>): Promise<T> => {
    const done = queue.current.then(job);
    queue.current = done.catch(() => {});
    return done;
  }, []);

  const keep = useCallback(
    (forTask: string, task: TaskDetail | undefined, events: EventView[]) => {
      if (shownId.current !== forTask) return;
      if (task) held.current = { taskId: forTask, task };
      setState((was) => {
        const same = was?.taskId === forTask;
        return {
          taskId: forTask,
          task: task ?? (same ? was.task : undefined),
          events: mergeEvents(same ? was.events : [], events),
        };
      });
    },
    [],
  );

  const load = useCallback(
    (whole: boolean) => {
      const pending = waiting.current;
      if (pending?.taskId === taskId) {
        pending.whole ||= whole;
        return pending.done;
      }
      const job = { taskId, whole, done: Promise.resolve() };
      job.done = enqueue(async () => {
        if (waiting.current === job) waiting.current = undefined;
        const mine =
          held.current?.taskId === taskId ? held.current.task : undefined;
        const open = mine ? heldOpen(mine) : [];
        try {
          if (job.whole || !mine || open.length > OPEN_HELD_MAX) {
            // As far back as the page holds: the part it was scrolled to stays.
            const from = mine
              ? (mine.earlier?.before ?? mine.created_at)
              : undefined;
            const task = await api.task(taskId, { from, types: SHOWN_EVENTS });
            keep(taskId, task, task.events ?? []);
          } else {
            const changes = await api.taskChanges(
              taskId,
              mine.last_event_seq,
              open,
            );
            keep(taskId, applyChanges(mine, changes), []);
          }
        } catch (err) {
          if (shownId.current !== taskId) return;
          setState((was) => ({
            ...(was?.taskId === taskId ? was : { taskId, events: [] }),
            error: err as Error,
          }));
        }
      });
      waiting.current = job;
      return job.done;
    },
    [api, taskId, enqueue, keep],
  );

  /** Loads the part before what the page holds; false if it could not. */
  const earlier = useCallback(
    () =>
      enqueue(async () => {
        const mine =
          held.current?.taskId === taskId ? held.current.task : undefined;
        if (!mine?.earlier) return true;
        try {
          const part = await api.taskEarlier(
            taskId,
            mine.earlier.before,
            SHOWN_EVENTS,
          );
          keep(taskId, applyEarlier(mine, part), part.events);
          return true;
        } catch {
          return false;
        }
      }),
    [api, taskId, enqueue, keep],
  );

  useEffect(() => {
    held.current = undefined;
    void load(true);
    const seen = () => {
      if (document.visibilityState === "visible") void load(true);
    };
    document.addEventListener("visibilitychange", seen);
    return () => document.removeEventListener("visibilitychange", seen);
  }, [load]);

  const mine = state?.taskId === taskId ? state : undefined;
  const live = useCallback(
    (events: EventView[]) => keep(taskId, undefined, events),
    [keep, taskId],
  );
  return {
    data: mine?.task,
    events: mine?.events ?? NO_EVENTS,
    error: mine?.error,
    load,
    earlier,
    live,
  };
}

const NO_EVENTS: EventView[] = [];

export default function Task() {
  const { taskId = "" } = useParams();
  const { t } = useI18n();
  const task = useTask(taskId);
  const { load, earlier, live } = task;
  const reload = useCallback(() => load(false), [load]);
  const { connected, saying } = useEvents(taskId, task.data?.last_event_seq, {
    onEvents: live,
    onChange: (whole) => void load(whole),
  });
  // The agent's text arrives with the task: load its formatter alongside, not after.
  useEffect(() => void loadMarkdown().catch(() => {}), []);

  if (task.error) {
    const missing = task.error instanceof ApiError && task.error.status === 404;
    return (
      <div className="flex min-w-0 flex-1 flex-col">
        <PageBar />
        <div className="mx-auto w-full max-w-[720px] p-6">
          {missing ? (
            <EmptyState title={t("detail.notFound")} />
          ) : (
            <ErrorState
              message={t("detail.error")}
              onRetry={() => void load(true)}
            />
          )}
        </div>
      </div>
    );
  }
  if (!task.data) {
    return (
      <div className="flex min-w-0 flex-1 flex-col">
        <PageBar />
        <Spinner />
      </div>
    );
  }
  return (
    <TaskView
      key={task.data.id}
      task={task.data}
      events={task.events}
      saying={saying}
      connected={connected}
      reload={reload}
      loadEarlier={earlier}
    />
  );
}

function TaskView({
  task,
  events,
  saying,
  connected,
  reload,
  loadEarlier,
}: {
  task: TaskDetail;
  events: EventView[];
  saying: SayingView | null;
  connected: boolean;
  reload: () => Promise<void>;
  /** Loads the part of the task before the one shown; false if it could not. */
  loadEarlier: () => Promise<boolean>;
}) {
  usePageTitle(task.title ?? task.request);
  const { t } = useI18n();
  const { api, me } = useAuth();
  const { layout } = useLayout();
  const { devices, refresh } = useWorkspace();
  const navigate = useNavigate();
  const all = devices.data ?? [];
  const items = useMemo(
    () => inPart(buildTimeline(task, events), task.earlier?.before),
    [task, events],
  );
  const finished = ["stopped", "completed", "failed"].includes(task.status);
  const [error, setError] = useState<string>();
  const [confirm, setConfirm] = useState<"stop" | "delete">();
  const [takeover, setTakeover] = useState<TakeoverView>();
  const [draft, setDraft] = useState<{ text: string; seq: number }>();
  const [takingOver, setTakingOver] = useState<string>();
  const tabs = usePanelTabs(task);
  const chats = useSideChats(task.id);
  const wide = layout === "desktop" || layout === "large";
  const panelWidth = usePanelWidth(layout === "large" ? 400 : 340);
  const [resizing, setResizing] = useState(false);
  const [panel, setPanel] = useState(() => {
    try {
      return localStorage.getItem("witx.panel") !== "closed";
    } catch {
      return true;
    }
  });
  const [sheet, setSheet] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const edges = useScrollEdges();
  const { ref: watchEdges } = edges;
  const attachScroller = useCallback(
    (el: HTMLDivElement | null) => {
      scroller.current = el;
      watchEdges(el);
    },
    [watchEdges],
  );
  const nearBottom = useRef(true);

  // The part before, loaded as the top of the conversation nears view.
  const [early, setEarly] = useState<"idle" | "loading" | "failed">("idle");
  const showEarlier = useCallback(() => {
    if (early === "loading") return;
    setEarly("loading");
    void loadEarlier().then((ok) => setEarly(ok ? "idle" : "failed"));
  }, [early, loadEarlier]);
  // Watched once the page has moved to the newest message (its first scroll), or at
  // once when there is nothing to scroll: not in the moment it opens at the top.
  const [settled, setSettled] = useState(false);
  const earlierMarker = useNearEnd(
    showEarlier,
    !!task.earlier && early === "idle" && settled,
    items.length,
    "top",
  );
  // What was in view stays there as the part before is put above it.
  const height = useRef(0);
  const shownFrom = useRef(task.earlier?.before);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (shownFrom.current !== task.earlier?.before) {
      shownFrom.current = task.earlier?.before;
      el.scrollTop += el.scrollHeight - height.current;
    }
    height.current = el.scrollHeight;
    if (el.scrollHeight <= el.clientHeight) setSettled(true);
  });

  const changed = async () => {
    await reload();
    refresh();
  };

  async function act(work: () => Promise<unknown>) {
    setError(undefined);
    try {
      await work();
      await changed();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  /** Pauses the task if needed, waits until every device confirmed it, then takes over. */
  async function takeOver(deviceId: string) {
    setError(undefined);
    setTakingOver(deviceId);
    setSheet(false);
    try {
      let current = task;
      if (current.status === "active") current = await api.pause(task.id);
      for (let i = 0; i < 60 && current.status === "pausing"; i++) {
        await new Promise((r) => setTimeout(r, 500));
        current = await api.task(task.id);
      }
      if (current.status !== "paused")
        throw new Error(t("detail.takeover.failed"));
      setTakeover(await api.takeover(task.id, { device_id: deviceId }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTakingOver(undefined);
      await changed();
    }
  }

  // "Take over" in the device pool, on a phone this task holds (?takeover=<device>).
  const [params, setParams] = useSearchParams();
  const wanted = params.get("takeover");
  const takeOverWanted = useEffectEvent((deviceId: string) => {
    setParams({}, { replace: true });
    void takeOver(deviceId);
  });
  useEffect(() => {
    if (wanted) takeOverWanted(wanted);
  }, [wanted]);

  // Keep the newest message in view while the reader is at the bottom.
  useEffect(() => {
    const el = scroller.current;
    if (el && nearBottom.current) el.scrollTop = el.scrollHeight;
  }, [items.length, task.result, task.status]);

  const togglePanel = () => {
    if (!wide) return setSheet(true);
    setPanel((p) => {
      try {
        localStorage.setItem("witx.panel", p ? "closed" : "open");
      } catch {
        // not remembered
      }
      return !p;
    });
  };

  const menu: MenuEntry[] = [
    ...(["active", "pausing", "paused"].includes(task.status)
      ? [
          {
            id: "stop",
            label: t("detail.stop"),
            icon: OctagonX,
            danger: true,
            onSelect: () => setConfirm("stop"),
          },
        ]
      : []),
    ...(finished
      ? [
          {
            id: "delete",
            label: t("detail.delete"),
            icon: Trash2,
            danger: true,
            onSelect: () => setConfirm("delete"),
          },
        ]
      : []),
  ];

  const closeTakeover = useCallback(() => {
    setTakeover(undefined);
    void changed();
  }, [changed]);

  /** Opens the side panel at a tab: beside the conversation, or as a sheet on phones. */
  const openPanel = (tab: PanelTab) => {
    tabs.open(tab);
    showPanel();
  };
  const showPanel = () => {
    if (!wide) return setSheet(true);
    if (!panel) togglePanel();
  };

  const panelContent = (
    <SidePanel
      task={task}
      items={items}
      onJump={wide ? undefined : () => setSheet(false)}
      devices={all}
      tabs={tabs}
      chats={chats}
      onChanged={changed}
      onTakeOver={(id) => void takeOver(id)}
      takingOver={takingOver}
    />
  );
  const panelOpen = !!(panel || takeover);

  return (
    <div className="flex min-w-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <PageBar>
          <div className="min-w-0 flex-1">
            <h1
              className="truncate text-[15px] font-semibold"
              title={task.request}
            >
              {task.title ?? task.request}
            </h1>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <TaskStatusChip
              status={task.status}
              waitingForPhone={waitsForPhone(task, all)}
            />
            {menu.length > 0 && (
              <Menu
                label={t("detail.actions")}
                entries={menu}
                trigger={({ ref, open, toggle, onKeyDown }) => (
                  <Button
                    ref={ref}
                    variant="ghost"
                    size="sm"
                    iconOnly
                    icon={MoreHorizontal}
                    aria-label={t("detail.actions")}
                    aria-haspopup="menu"
                    aria-expanded={open}
                    onClick={toggle}
                    onKeyDown={onKeyDown}
                  />
                )}
              />
            )}
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              icon={PanelRight}
              aria-label={t(
                panel && wide ? "detail.hidePanel" : "detail.showPanel",
              )}
              aria-pressed={wide ? panel : undefined}
              onClick={togglePanel}
            />
          </div>
        </PageBar>

        {!connected && (
          <div
            role="status"
            className="flex items-center justify-center gap-2 border-b border-line bg-slate-soft px-4 py-1.5 text-[12.5px] font-medium"
          >
            <Ico icon={WifiOff} size={14} className="anim-pulse" />
            {t("conn.lost")}
          </div>
        )}

        {/* Softened where it scrolls out of sight under the bar and above the
            composer. */}
        <div className="relative flex min-h-0 flex-1 flex-col">
          <Fade side="top" show={edges.above} />
          <Fade side="bottom" show={edges.below} />
          <div
            ref={attachScroller}
            onScroll={(e) => {
              const el = e.currentTarget;
              nearBottom.current =
                el.scrollHeight - el.scrollTop - el.clientHeight < 120;
              setSettled(true);
            }}
            className="scroll-quiet min-h-0 flex-1 overflow-y-auto"
          >
            <div className="mx-auto w-full max-w-[760px] space-y-4 px-4 pt-5 pb-6">
              {task.permission_mode === "full" && !finished && (
                <p
                  role="status"
                  className="flex items-center gap-2 rounded-lg border border-amber/40 bg-amber-soft px-3 py-2 text-[13px] text-amber"
                >
                  <Ico icon={ShieldOff} size={15} />
                  {t("detail.fullOn")}
                </p>
              )}
              {error && <ErrorState message={error} />}
              {task.earlier &&
                (early === "failed" ? (
                  <Button variant="ghost" onClick={showEarlier}>
                    {t("conversation.earlier")}
                  </Button>
                ) : (
                  <div ref={earlierMarker} className="py-1">
                    {early === "loading" && (
                      <div
                        role="status"
                        aria-label={t("common.loading")}
                        className="skeleton h-4 w-2/5 rounded"
                      />
                    )}
                  </div>
                ))}
              <Conversation
                task={task}
                items={items}
                devices={all}
                saying={saying}
                onChange={changed}
                onTakeOver={(id) => void takeOver(id)}
                onEdit={(text) =>
                  setDraft((d) => ({ text, seq: (d?.seq ?? 0) + 1 }))
                }
              />
            </div>
          </div>
        </div>

        <div className="mx-auto w-full max-w-[760px] px-4 pb-[max(1.5rem,calc(env(safe-area-inset-bottom)+0.5rem))]">
          {/* Only a task with a goal has one to show; most are a plain request. */}
          {task.goal && (
            <GoalBar task={task} onOpen={() => openPanel("goal")} />
          )}
          <TaskComposer
            task={task}
            draft={draft}
            onChanged={changed}
            onCommand={(command) =>
              command === "side"
                ? (tabs.newChat(), showPanel())
                : setConfirm("stop")
            }
          />
        </div>
      </div>

      {/* Opens and closes by its width; the content keeps its own, so nothing
          reflows while it moves. */}
      {wide && (
        <aside
          aria-label={t("panel.title")}
          aria-hidden={!panelOpen}
          inert={!panelOpen}
          style={{ width: panelOpen ? panelWidth.width : 0 }}
          className={cn(
            "relative shrink-0 overflow-hidden border-l bg-bg",
            !resizing &&
              "transition-[width,border-color] duration-300 ease-[var(--ease-sheet)]",
            panelOpen ? "border-line" : "border-transparent",
          )}
        >
          {panelOpen && (
            <PanelResizer
              width={panelWidth.width}
              onResize={panelWidth.set}
              onReset={panelWidth.reset}
              onDragging={setResizing}
            />
          )}
          <div
            style={{ width: panelWidth.width }}
            className={cn(
              "h-full transition-[opacity,translate] duration-300 ease-[var(--ease-sheet)]",
              panelOpen ? "opacity-100" : "translate-x-4 opacity-0",
            )}
          >
            {takeover ? (
              <div className="scroll-quiet h-full overflow-y-auto">
                <Takeover
                  key={takeover.takeover_id}
                  inline
                  takeover={takeover}
                  device={all.find((d) => d.id === takeover.device_id)}
                  onClosed={closeTakeover}
                  onAgain={() => void takeOver(takeover.device_id)}
                />
              </div>
            ) : (
              panelContent
            )}
          </div>
        </aside>
      )}
      {!wide && (
        <Sheet
          open={sheet}
          onClose={() => setSheet(false)}
          title={t("panel.title")}
        >
          <div className="h-[78dvh]">{panelContent}</div>
        </Sheet>
      )}

      {takeover && !wide && (
        <Takeover
          key={takeover.takeover_id}
          takeover={takeover}
          device={all.find((d) => d.id === takeover.device_id)}
          onClosed={closeTakeover}
          onAgain={() => void takeOver(takeover.device_id)}
        />
      )}

      <Dialog
        open={confirm === "stop"}
        onClose={() => setConfirm(undefined)}
        tone="red"
        title={t("detail.stop.title")}
        footer={
          <>
            <Button data-autofocus onClick={() => setConfirm(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setConfirm(undefined);
                void act(() => api.stop(task.id));
              }}
            >
              {t("detail.stop")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{t("detail.stop.body")}</p>
      </Dialog>
      <Dialog
        open={confirm === "delete"}
        onClose={() => setConfirm(undefined)}
        tone="red"
        title={t("detail.delete.title")}
        footer={
          <>
            <Button data-autofocus onClick={() => setConfirm(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={async () => {
                setConfirm(undefined);
                setError(undefined);
                try {
                  await api.deleteTask(task.id);
                  refresh();
                  navigate("/", { replace: true });
                } catch (err) {
                  setError((err as Error).message);
                }
              }}
            >
              {t("detail.delete")}
            </Button>
          </>
        }
      >
        <p className="text-fg">
          {t("detail.delete.body", { days: me?.audit_retention_days ?? 0 })}
        </p>
      </Dialog>
    </div>
  );
}

/** The side panel's left edge, dragged to make the panel wider or narrower (or moved
 * with the arrow keys when focused); a double click goes back to the default width. */
function PanelResizer({
  width,
  onResize,
  onReset,
  onDragging,
}: {
  width: number;
  onResize: (width: number) => void;
  onReset: () => void;
  onDragging: (dragging: boolean) => void;
}) {
  const { t } = useI18n();
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t("panel.resize")}
      aria-valuenow={width}
      aria-valuemin={PANEL_MIN}
      tabIndex={0}
      title={t("panel.resize")}
      onPointerDown={(e) => {
        const start = e.clientX;
        const from = width;
        const handle = e.currentTarget;
        handle.setPointerCapture(e.pointerId);
        onDragging(true);
        const move = (m: PointerEvent) => onResize(from + (start - m.clientX));
        const up = () => {
          onDragging(false);
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", up);
          handle.removeEventListener("pointercancel", up);
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
        handle.addEventListener("pointercancel", up);
      }}
      onDoubleClick={onReset}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") onResize(width + 16);
        else if (e.key === "ArrowRight") onResize(width - 16);
        else return;
        e.preventDefault();
      }}
      className="group absolute inset-y-0 left-0 z-10 w-2 cursor-col-resize touch-none outline-none"
    >
      <span
        aria-hidden
        className="absolute inset-y-0 left-0 w-0.5 bg-accent opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 group-active:opacity-100"
      />
    </div>
  );
}
