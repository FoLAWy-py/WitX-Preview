import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  Bot,
  Check,
  ChevronDown,
  MessagesSquare,
  Plus,
  Smartphone,
  Target,
  X,
  type LucideIcon,
} from "lucide-react";
import type { DeviceView, TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/format";
import { newId } from "@/lib/id";
import { agentName } from "@/lib/agents";
import type { TimelineItem } from "@/lib/timeline";
import { DevicePanel } from "./DevicePanel";
import { unavailable } from "@/lib/pool";
import { Button, Ico, PlatformIcon } from "./ui/base";
import { Menu, type MenuEntry } from "./ui/popover";
import { GoalTab } from "./GoalTab";
import { ChatTab, SideChats } from "./SideChatTab";
import { AgentTab } from "./AgentTab";

/** A tab of the side panel: the phones, the goal, one side chat, or a sub-agent. */
export type PanelTab = "phone" | "goal" | `chat:${string}` | `agent:${string}`;

const chatId = (tab: PanelTab | undefined) =>
  tab?.startsWith("chat:") ? tab.slice(5) : undefined;
const agentId = (tab: PanelTab | undefined) =>
  tab?.startsWith("agent:") ? tab.slice(6) : undefined;

/**
 * Which pages the side panel shows for a task, and which one is in front. Nothing is
 * open uninvited: a task with phones starts with its phones, one without starts empty,
 * and the goal or a side chat open when the person asks. Kept per task in this browser.
 */
export function usePanelTabs(task: TaskDetail) {
  const key = `witx.panel.${task.id}`;
  const [state, setState] = useState<{ tabs: PanelTab[]; active?: PanelTab }>(
    () => {
      try {
        const kept = JSON.parse(localStorage.getItem(key) ?? "null") as {
          tabs?: PanelTab[];
          active?: PanelTab;
        } | null;
        if (kept && Array.isArray(kept.tabs))
          return { tabs: kept.tabs, active: kept.active };
      } catch {
        // nothing kept
      }
      const tabs: PanelTab[] = task.device_ids.length ? ["phone"] : [];
      return { tabs, active: tabs[0] };
    },
  );
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(state));
    } catch {
      // not kept
    }
  }, [key, state]);
  const open = useCallback((tab: PanelTab) => {
    setState((s) => ({
      tabs: s.tabs.includes(tab) ? s.tabs : [...s.tabs, tab],
      active: tab,
    }));
  }, []);
  return {
    tabs: state.tabs,
    active: state.active,
    open,
    select: (tab: PanelTab) => setState((s) => ({ ...s, active: tab })),
    close: (tab: PanelTab) =>
      setState((s) => {
        const at = s.tabs.indexOf(tab);
        const tabs = s.tabs.filter((x) => x !== tab);
        const active =
          s.active === tab ? tabs[Math.min(at, tabs.length - 1)] : s.active;
        return { tabs, active };
      }),
    /** Opens a new, empty side chat in front; returns its tab. */
    newChat: () => {
      const tab: PanelTab = `chat:${newId()}`;
      open(tab);
      return tab;
    },
  };
}
export type PanelTabs = ReturnType<typeof usePanelTabs>;

function TabButton({
  tab,
  selected,
  icon,
  label,
  onSelect,
  onClose,
  closeLabel,
}: {
  tab: PanelTab;
  selected: boolean;
  icon: LucideIcon;
  label: string;
  onSelect: () => void;
  onClose: () => void;
  closeLabel: string;
}) {
  return (
    <div
      className={cn(
        // Like a browser's tabs: as wide as their name, up to a limit, and shrinking as
        // more open, down to an icon and a word.
        "group flex h-8 max-w-[200px] min-w-[84px] flex-[0_1_auto] items-center rounded-lg text-[13px] font-medium pointer-coarse:h-10",
        selected
          ? "bg-selected text-fg"
          : "text-muted hover:bg-hovered hover:text-fg",
      )}
    >
      <button
        type="button"
        role="tab"
        data-tab={tab}
        aria-selected={selected}
        tabIndex={selected ? 0 : -1}
        title={label}
        aria-keyshortcuts="Delete"
        onClick={onSelect}
        onAuxClick={(e) => e.button === 1 && onClose()}
        className="flex h-full min-w-0 items-center gap-1.5 pr-0.5 pl-2"
      >
        <Ico icon={icon} size={14} className="shrink-0" />
        <span className="truncate">{label}</span>
      </button>
      {/* For the pointer; from the keyboard, Delete on the tab closes it. A tab list
          holds only tabs, so this is hidden from assistive technology. */}
      <button
        type="button"
        tabIndex={-1}
        aria-hidden
        title={closeLabel}
        onClick={onClose}
        className={cn(
          "mr-1 flex h-5 w-5 shrink-0 items-center justify-center rounded text-faint hover:bg-line hover:text-fg pointer-coarse:h-7 pointer-coarse:w-7",
          // Out of the way until wanted, except on touch where there is no hover.
          !selected &&
            "opacity-0 group-hover:opacity-100 pointer-coarse:opacity-100",
        )}
      >
        <X size={12} aria-hidden />
      </button>
    </div>
  );
}

/** Arrow keys move along the tabs, Home and End to either end (WAI-ARIA tabs);
 * Delete closes the tab and focus moves to the one shown next. */
function moveAlongTabs(
  e: KeyboardEvent<HTMLDivElement>,
  pick: (tab: PanelTab) => void,
  close: (tab: PanelTab) => void,
) {
  const strip = e.currentTarget;
  const tabs = [...strip.querySelectorAll<HTMLElement>("[data-tab]")];
  const at = tabs.indexOf(document.activeElement as HTMLElement);
  if (at < 0) return;
  if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    close(tabs[at].dataset.tab as PanelTab);
    requestAnimationFrame(() =>
      strip.querySelector<HTMLElement>("[aria-selected=true]")?.focus(),
    );
    return;
  }
  const next =
    e.key === "ArrowRight"
      ? (at + 1) % tabs.length
      : e.key === "ArrowLeft"
        ? (at - 1 + tabs.length) % tabs.length
        : e.key === "Home"
          ? 0
          : e.key === "End"
            ? tabs.length - 1
            : -1;
  if (next < 0) return;
  e.preventDefault();
  tabs[next].focus();
  pick(tabs[next].dataset.tab as PanelTab);
}

/** A task without phones: the pool's ready phones, to add one to the task. */
function AddPhones({
  task,
  devices,
  onChanged,
}: {
  task: TaskDetail;
  devices: DeviceView[];
  onChanged: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  async function add(id: string) {
    setBusy(id);
    setError(undefined);
    try {
      await api.setDevices(task.id, [...task.device_ids, id]);
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  }
  return (
    <div className="space-y-3 p-4">
      <p className="text-[13.5px] text-muted">{t("panel.noPhones")}</p>
      <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface">
        {devices.map((d) => {
          const why = unavailable(d, t);
          return (
            <li key={d.id} className="flex items-center gap-3 px-3 py-2.5">
              <PlatformIcon
                platform={d.platform}
                size={16}
                className={why ? "text-faint" : "text-muted"}
              />
              <span className="min-w-0 flex-1">
                <span
                  className={cn(
                    "block truncate text-[14px]",
                    why && "text-muted",
                  )}
                >
                  {d.name}
                </span>
                {(why || d.task_id) && (
                  <span className="block truncate text-[12px] text-faint">
                    {why ?? t("reason.busy")}
                  </span>
                )}
              </span>
              <Button
                size="sm"
                icon={Plus}
                disabled={!!why}
                busy={busy === d.id}
                onClick={() => void add(d.id)}
              >
                {t("panel.addPhone")}
              </Button>
            </li>
          );
        })}
      </ul>
      {error && (
        <p role="alert" className="text-[13px] text-red">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * The side panel, like Claude's: pages the person opens as tabs (the phones, the goal,
 * side chats), each closable, and "+" for any of them. Empty, it says what can go here.
 */
export function SidePanel({
  task,
  items,
  onJump,
  devices,
  tabs,
  chats,
  onChanged,
  onTakeOver,
  takingOver,
}: {
  task: TaskDetail;
  items: TimelineItem[];
  /** Before going to an item of the conversation (a sheet closes first). */
  onJump?: () => void;
  devices: DeviceView[];
  tabs: PanelTabs;
  chats: SideChats;
  onChanged: () => Promise<void>;
  onTakeOver: (deviceId: string) => void;
  takingOver?: string;
}) {
  const { t } = useI18n();
  const current = tabs.active;
  // Too many tabs to fit even shrunk: the strip scrolls, and one button lists them all.
  const titles = chats.messages.map((m) => m.thread_id).join();
  const strip = useRef<HTMLDivElement>(null);
  const [crowded, setCrowded] = useState(false);
  // The tab in front is always in sight: when it changes, when titles load, and when
  // the strip changes size (a sheet that is still opening).
  useLayoutEffect(() => {
    const el = strip.current;
    if (!el) return;
    const bring = () =>
      el
        .querySelector<HTMLElement>(`[data-tab="${current}"]`)
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    const check = () => {
      setCrowded(el.scrollWidth > el.clientWidth + 1);
      bring();
    };
    const frame = requestAnimationFrame(check);
    const watch = new ResizeObserver(check);
    watch.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      watch.disconnect();
    };
  }, [current, tabs.tabs.length, titles]);
  const titleOf = (id: string) =>
    chats.messages.find((m) => m.thread_id === id)?.question ?? t("side.new");
  const meta: Record<"phone" | "goal", { icon: LucideIcon; label: string }> = {
    phone: { icon: Smartphone, label: t("panel.tab.phone") },
    goal: { icon: Target, label: t("goal.title") },
  };
  const agentOf = (id: string) => task.runs.find((r) => r.id === id);
  const about = (tab: PanelTab) => {
    if (chatId(tab))
      return { icon: MessagesSquare, label: titleOf(chatId(tab)!) };
    if (agentId(tab)) {
      const run = agentOf(agentId(tab)!);
      return {
        icon: Bot,
        label: run
          ? agentName(run, task.runs, devices, t)
          : t("agents.numbered", { n: 1 }),
      };
    }
    return meta[tab as "phone" | "goal"];
  };
  const putAway = chats.threads.filter(
    (id) => !tabs.tabs.includes(`chat:${id}`),
  );

  const entries: MenuEntry[] = [
    ...(tabs.tabs.includes("phone")
      ? []
      : [
          {
            id: "phone",
            label: meta.phone.label,
            icon: Smartphone,
            onSelect: () => tabs.open("phone"),
          },
        ]),
    ...(tabs.tabs.includes("goal")
      ? []
      : [
          {
            id: "goal",
            label: meta.goal.label,
            icon: Target,
            onSelect: () => tabs.open("goal"),
          },
        ]),
    {
      id: "chat",
      label: t("panel.newChat"),
      icon: MessagesSquare,
      onSelect: () => void tabs.newChat(),
    },
    ...(putAway.length
      ? [
          { id: "sep", separator: true as const },
          { id: "earlier", header: t("panel.earlierChats") },
          ...putAway.map((id) => ({
            id: `chat-${id}`,
            label: titleOf(id),
            icon: MessagesSquare,
            onSelect: () => tabs.open(`chat:${id}`),
          })),
        ]
      : []),
  ];

  let body: ReactNode;
  if (!current)
    body = (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <p className="max-w-[240px] text-[13.5px] text-muted">
          {t("panel.empty")}
        </p>
        <div className="flex flex-wrap justify-center gap-2">
          <Button
            size="sm"
            icon={Smartphone}
            onClick={() => tabs.open("phone")}
          >
            {meta.phone.label}
          </Button>
          <Button size="sm" icon={Target} onClick={() => tabs.open("goal")}>
            {meta.goal.label}
          </Button>
          <Button
            size="sm"
            icon={MessagesSquare}
            onClick={() => void tabs.newChat()}
          >
            {t("panel.newChat")}
          </Button>
        </div>
      </div>
    );
  else if (current === "goal")
    body = (
      <GoalTab
        task={task}
        items={items}
        onChanged={onChanged}
        onJump={onJump}
      />
    );
  else if (chatId(current))
    body = (
      <ChatTab
        key={current}
        thread={chatId(current)!}
        chats={chats}
        onDeleted={() => tabs.close(current)}
      />
    );
  else if (agentId(current) && agentOf(agentId(current)!))
    body = (
      <AgentTab
        key={current}
        task={task}
        run={agentOf(agentId(current)!)!}
        items={items}
        devices={devices}
        onChanged={onChanged}
        onTakeOver={onTakeOver}
      />
    );
  else if (task.device_ids.length === 0)
    body = <AddPhones task={task} devices={devices} onChanged={onChanged} />;
  else
    body = (
      <DevicePanel
        task={task}
        devices={devices}
        onTakeOver={onTakeOver}
        takingOver={takingOver}
        onOpenAgent={(id) => tabs.open(`agent:${id}`)}
      />
    );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-line px-2 py-2">
        <div
          ref={strip}
          role="tablist"
          aria-label={t("panel.title")}
          onKeyDown={(e) => moveAlongTabs(e, tabs.select, tabs.close)}
          className={cn(
            "tab-strip flex min-w-0 flex-1 items-center gap-1",
            crowded && "tab-strip-crowded",
          )}
        >
          {tabs.tabs.map((tab) => {
            const { icon, label } = about(tab);
            return (
              <TabButton
                key={tab}
                tab={tab}
                selected={current === tab}
                icon={icon}
                label={label}
                onSelect={() => tabs.select(tab)}
                onClose={() => tabs.close(tab)}
                closeLabel={t("panel.closeTab", { name: label })}
              />
            );
          })}
        </div>
        {crowded && (
          <Menu
            label={t("panel.allTabs")}
            placement="bottom-end"
            minWidth={240}
            entries={tabs.tabs.map((tab) => {
              const { icon, label } = about(tab);
              return {
                id: tab,
                label,
                icon: tab === current ? Check : icon,
                onSelect: () => tabs.select(tab),
              };
            })}
            trigger={({ ref, open, toggle, onKeyDown }) => (
              <Button
                ref={ref}
                variant="ghost"
                size="sm"
                iconOnly
                icon={ChevronDown}
                aria-label={t("panel.allTabs")}
                title={t("panel.allTabs")}
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={toggle}
                onKeyDown={onKeyDown}
              />
            )}
          />
        )}
        <Menu
          label={t("panel.open")}
          placement="bottom-end"
          minWidth={220}
          entries={entries}
          trigger={({ ref, open, toggle, onKeyDown }) => (
            <Button
              ref={ref}
              variant="ghost"
              size="sm"
              iconOnly
              icon={Plus}
              aria-label={t("panel.open")}
              title={t("panel.open")}
              aria-haspopup="menu"
              aria-expanded={open}
              onClick={toggle}
              onKeyDown={onKeyDown}
            />
          )}
        />
      </div>
      <div
        role="tabpanel"
        className={cn(
          "min-h-0 flex-1",
          chatId(current) ? "flex flex-col" : "scroll-quiet overflow-y-auto",
        )}
      >
        {body}
      </div>
    </div>
  );
}
