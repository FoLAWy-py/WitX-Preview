import {
  useCallback,
  useEffect,
  useEffectEvent,
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
import { buildTimeline } from "@/lib/timeline";
import { useAsync } from "@/lib/useAsync";
import { usePageTitle } from "@/lib/title";
import { useWorkspace } from "@/lib/workspace";
import { TaskComposer } from "@/components/Composer";
import { Conversation } from "@/components/Conversation";
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

/**
 * Follows the task's event stream and refreshes the task after each burst of events,
 * and after every reconnect: changes made while disconnected (an approval expiring,
 * say) are not all events, so the whole task is fetched again.
 */
/** How long the agent's live words stay once its step is over: the message kept for
 * them arrives with the task fetched after the step's events, a moment later. */
const SAID_MS = 2000;

function useEvents(taskId: string, refresh: () => Promise<void>) {
  const { api } = useAuth();
  const [events, setEvents] = useState<EventView[]>([]);
  const [saying, setSaying] = useState<SayingView | null>(null);
  const [connected, setConnected] = useState(true);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    let burst: ReturnType<typeof setTimeout> | undefined;
    let said: ReturnType<typeof setTimeout> | undefined;
    let offline: ReturnType<typeof setTimeout> | undefined;
    setEvents([]);
    setSaying(null);
    setConnected(true);
    const stop = follow(
      (after, handlers, signal) => api.events(taskId, after, handlers, signal),
      {
        onEvent: (event) => {
          // An event seen before (a resent stream) is not shown twice.
          setEvents((list) =>
            list.some((e) => e.seq === event.seq) ? list : [...list, event],
          );
          clearTimeout(burst);
          burst = setTimeout(() => void refreshRef.current(), 150);
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
          if (reconnected) void refreshRef.current();
        },
      },
    );
    return () => {
      stop();
      clearTimeout(burst);
      clearTimeout(said);
      clearTimeout(offline);
    };
  }, [api, taskId]);

  return { events, connected, saying };
}

export default function Task() {
  const { taskId = "" } = useParams();
  const { api } = useAuth();
  const { t } = useI18n();
  const task = useAsync(() => api.task(taskId), [api, taskId]);
  const { events, connected, saying } = useEvents(taskId, task.reload);

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
              onRetry={() => void task.reload()}
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
      events={events}
      saying={saying}
      connected={connected}
      reload={task.reload}
    />
  );
}

function TaskView({
  task,
  events,
  saying,
  connected,
  reload,
}: {
  task: TaskDetail;
  events: EventView[];
  saying: SayingView | null;
  connected: boolean;
  reload: () => Promise<void>;
}) {
  usePageTitle(task.title ?? task.request);
  const { t } = useI18n();
  const { api, me } = useAuth();
  const { layout } = useLayout();
  const { devices, refresh } = useWorkspace();
  const navigate = useNavigate();
  const all = devices.data ?? [];
  const items = useMemo(() => buildTimeline(task, events), [task, events]);
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
