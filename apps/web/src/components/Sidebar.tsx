import { useState, type ReactNode, useDeferredValue } from "react";
import { Link, useLocation, useNavigate, NavLink } from "react-router";
import {
  Check,
  ChevronsUpDown,
  Languages,
  LogOut,
  Monitor,
  Moon,
  Settings as SettingsIcon,
  Sun,
  CalendarClock,
  ChevronDown,
  ChevronRight,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pin,
  PinOff,
  Pencil,
  Plus,
  Puzzle,
  Search,
  Smartphone,
  Trash2,
  Folder,
  FolderMinus,
  FolderPlus,
  type LucideIcon,
} from "lucide-react";
import type { TaskGroupView, TaskSummary, DeviceView } from "@/api/types.gen";
import { LOCALES, useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { Fade, useScrollEdges } from "./ui/fade";
import { cn } from "@/lib/format";
import { useTheme } from "@/lib/prefs";
import { Button, Field, Ico, PlatformIcon, inputClass } from "./ui/base";
import { Dialog } from "./ui/dialog";
import { Menu, type MenuEntry } from "./ui/popover";
import { useTaskList, useWorkspace } from "@/lib/workspace";
import { readyFirst, unavailable } from "@/lib/pool";
import { scheduleNotice, scheduleState } from "@/lib/schedules";
import { StatusDot } from "./ui/status";
import { Logo } from "./Logo";

const TASK_PAGE = 20;
const GROUP_PAGE = 10;
const PINNED_PAGE = 50;
const DEVICES_SHOWN = 6;

/** The 20 px column every sidebar row starts with (new task, search, schedules, phones,
 * tasks), so their text starts on one vertical line whatever the icon's size. */
function Lead({ children }: { children?: ReactNode }) {
  return (
    <span className="flex h-5 w-5 shrink-0 items-center justify-center">
      {children}
    </span>
  );
}

/** A quiet row under a list: "Show 3 more" (a chevron down) or a folded group such as
 * "Paused · 2" (a chevron right). Its text starts on the rows' line. */
function ListToggle({
  expanded,
  fold,
  onClick,
  disabled,
  children,
}: {
  expanded: boolean;
  /** A group that folds away, rather than more of the same list. */
  fold?: boolean;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  const Chevron = fold ? ChevronRight : ChevronDown;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-expanded={expanded}
      className={cn(
        "flex h-7 w-full items-center gap-2.5 rounded-lg px-2.5 text-[12.5px] text-faint hover:bg-hovered hover:text-fg pointer-coarse:min-h-11",
        // A group stands a little apart from the list above it.
        fold ? "mt-1.5" : "mt-0.5",
      )}
    >
      <Lead>
        <Chevron
          size={13}
          strokeWidth={1.75}
          aria-hidden
          className={cn(
            "transition-transform",
            expanded && (fold ? "rotate-90" : "rotate-180"),
          )}
        />
      </Lead>
      <span className="truncate">{children}</span>
    </button>
  );
}

/** A folded state kept in this browser (wrapped: storage may be unavailable). */
function useRemembered(key: string, initial: boolean): [boolean, () => void] {
  const [open, setOpen] = useState(() => {
    try {
      const kept = localStorage.getItem(key);
      return kept === null ? initial : kept !== "closed";
    } catch {
      return initial;
    }
  });
  const toggle = () =>
    setOpen((o) => {
      try {
        localStorage.setItem(key, o ? "closed" : "open");
      } catch {
        // not remembered
      }
      return !o;
    });
  return [open, toggle];
}

function TaskRow({
  task,
  actions,
}: {
  task: TaskSummary;
  actions: TaskActions;
}) {
  const { t } = useI18n();
  const finished = ["stopped", "completed", "failed"].includes(task.status);
  const entries: MenuEntry[] = [
    {
      id: "pin",
      label: t(task.pinned ? "pins.unpin" : "pins.pin"),
      icon: task.pinned ? PinOff : Pin,
      onSelect: () => actions.pin(task, !task.pinned),
    },
    { id: "s0", separator: true },
    { id: "h-move", header: t("groups.moveTo") },
    ...actions.groups
      .filter((g) => g.id !== task.group_id)
      .map((g) => ({
        id: `g-${g.id}`,
        label: g.name,
        icon: Folder,
        onSelect: () => actions.move(task, g.id),
      })),
    ...(task.group_id
      ? [
          {
            id: "ungroup",
            label: t("groups.none"),
            icon: FolderMinus,
            onSelect: () => actions.move(task, null),
          },
        ]
      : []),
    {
      id: "new-group",
      label: t("groups.newWithTask"),
      icon: FolderPlus,
      onSelect: () => actions.newGroupFor(task),
    },
    { id: "s1", separator: true },
    {
      id: "delete",
      label: t("detail.delete"),
      icon: Trash2,
      danger: true,
      disabled: !finished,
      hint: finished ? undefined : t("sidebar.deleteFinishedOnly"),
      onSelect: () => actions.remove(task),
    },
  ];
  return (
    <li className="group/row relative">
      <NavLink
        to={`/tasks/${task.id}`}
        onClick={actions.onPick}
        title={task.request}
        className={({ isActive }) =>
          cn(
            "flex h-8 items-center gap-2.5 rounded-lg pr-8 pl-2.5 text-[13.5px] pointer-coarse:min-h-11",
            isActive
              ? "bg-selected font-medium text-fg"
              : "text-muted hover:bg-hovered hover:text-fg",
          )
        }
      >
        <Lead>
          <StatusDot status={task.status} needsYou={task.needs_attention} />
        </Lead>
        <span className="min-w-0 flex-1 truncate">
          {task.title ?? task.request}
        </span>
        {task.needs_attention && (
          <span className="sr-only">{t("status.needsYou")}</span>
        )}
      </NavLink>
      <Menu
        label={t("sidebar.taskMenu")}
        entries={entries}
        minWidth={220}
        trigger={({ ref, open, toggle, onKeyDown }) => (
          <button
            ref={ref}
            type="button"
            aria-haspopup="menu"
            aria-expanded={open}
            aria-label={t("sidebar.taskMenuFor", {
              goal: task.title ?? task.request,
            })}
            onClick={toggle}
            onKeyDown={onKeyDown}
            className={cn(
              "absolute top-1/2 right-1 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-full text-faint hover:bg-line hover:text-fg",
              "opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 aria-expanded:opacity-100 pointer-coarse:opacity-100",
            )}
          >
            <MoreHorizontal size={15} strokeWidth={1.75} aria-hidden />
          </button>
        )}
      />
    </li>
  );
}

/** A list of tasks that loads more on request. */
function TaskList({
  filter,
  pageSize,
  empty,
  actions,
}: {
  filter: { group?: string; q?: string; pinned?: "only" | "without" };
  pageSize: number;
  empty: string;
  actions: TaskActions;
}) {
  const { t } = useI18n();
  const list = useTaskList(filter, pageSize);
  if (!list.data)
    return list.error ? (
      <p className="px-2.5 py-2 text-[13px] text-red">
        {t("sidebar.loadFailed")}
      </p>
    ) : (
      <div className="space-y-1.5 px-2.5 py-1.5">
        <div className="skeleton h-4 w-4/5 rounded" />
        <div className="skeleton h-4 w-3/5 rounded" />
      </div>
    );
  if (!list.data.length)
    // From the line the tasks' names start on: after the 20 px icon column.
    return (
      <p className="py-1.5 pr-2.5 pl-10 text-[13px] text-faint">{empty}</p>
    );
  return (
    <>
      <ul className="space-y-px">
        {list.data.map((task) => (
          <TaskRow key={task.id} task={task} actions={actions} />
        ))}
      </ul>
      {list.more && (
        <ListToggle
          expanded={false}
          onClick={list.showMore}
          disabled={list.loading}
        >
          {list.loading ? t("common.loading") : t("sidebar.showMore")}
        </ListToggle>
      )}
    </>
  );
}

function DeviceList({
  devices,
  onPick,
}: {
  devices: DeviceView[];
  onPick?: () => void;
}) {
  const { t } = useI18n();
  // Six at first, six more at a time.
  const [count, setCount] = useState(DEVICES_SHOWN);
  if (!devices.length)
    return (
      <p className="px-2.5 py-2 text-[13px] text-faint">
        {t("sidebar.noDevices")}
      </p>
    );
  const ordered = readyFirst(devices);
  const shown = ordered.slice(0, count);
  const left = ordered.length - shown.length;
  return (
    <>
      <ul className="space-y-px">
        {shown.map((d) => {
          const problem = unavailable(d, t);
          return (
            <li key={d.id}>
              <NavLink
                // A phone at work opens the task it works for.
                to={d.task_id ? `/tasks/${d.task_id}` : `/devices#${d.id}`}
                onClick={onPick}
                title={
                  problem ??
                  (d.task_id
                    ? t("devices.openTask")
                    : t("devices.status.ready"))
                }
                className="flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] text-muted hover:bg-hovered hover:text-fg pointer-coarse:min-h-11"
              >
                <Lead>
                  <PlatformIcon platform={d.platform} size={15} />
                </Lead>
                <span className="min-w-0 flex-1 truncate">{d.name}</span>
                <span
                  aria-hidden
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full",
                    problem
                      ? d.enabled
                        ? "bg-amber"
                        : "bg-slate/50"
                      : d.task_id
                        ? "bg-accent"
                        : "bg-green",
                  )}
                />
              </NavLink>
            </li>
          );
        })}
      </ul>
      {ordered.length > DEVICES_SHOWN && (
        <ListToggle
          expanded={left === 0}
          onClick={() =>
            setCount((c) => (left === 0 ? DEVICES_SHOWN : c + DEVICES_SHOWN))
          }
        >
          {left === 0
            ? t("sidebar.showLess")
            : t("sidebar.showMoreCount", {
                count: Math.min(left, DEVICES_SHOWN),
              })}
        </ListToggle>
      )}
    </>
  );
}

/** A sidebar heading that folds its list away; the choice is remembered. */
function Section({
  id,
  title,
  icon,
  extra,
  children,
}: {
  id: string;
  title: string;
  icon?: LucideIcon;
  extra?: ReactNode;
  children: ReactNode;
}) {
  const [open, toggle] = useRemembered(`witx.sidebar.${id}`, true);
  return (
    <section className="mb-4">
      <div className="flex items-center justify-between gap-1 pr-1">
        <h2 className="min-w-0">
          <button
            type="button"
            aria-expanded={open}
            onClick={toggle}
            className="group flex h-7 min-w-0 items-center gap-1.5 rounded-md px-2.5 text-[12px] font-medium text-faint hover:text-fg"
          >
            {icon && <Ico icon={icon} size={13} />}
            <span className="truncate">{title}</span>
            <ChevronRight
              size={13}
              strokeWidth={1.75}
              aria-hidden
              className={cn(
                "opacity-0 transition-transform group-hover:opacity-100 group-focus-visible:opacity-100",
                open && "rotate-90",
                !open && "opacity-100",
              )}
            />
          </button>
        </h2>
        {extra}
      </div>
      {open && children}
    </section>
  );
}

/** The left column: a new task, search, the schedules, the phones, the task groups, the
 * tasks and the account. */
export function Sidebar({
  collapsed,
  onToggle,
  onPick,
}: {
  collapsed?: boolean;
  onToggle?: () => void;
  onPick?: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const { attention, devices, groups, refresh, schedules } = useWorkspace();
  const navigate = useNavigate();
  const edges = useScrollEdges();
  const onSchedules = (schedules.data ?? []).filter(
    (s) => scheduleState(s) === "active",
  );
  const activeSchedules = onSchedules.length;
  const waitingSchedules = onSchedules.some(
    (s) => scheduleNotice(s) === "waiting",
  );
  const location = useLocation();
  const [query, setQuery] = useState("");
  const search = useDeferredValue(query.trim());
  const [naming, setNaming] = useState<{
    group?: TaskGroupView;
    task?: TaskSummary;
  }>();
  const [name, setName] = useState("");
  const [deletingGroup, setDeletingGroup] = useState<TaskGroupView>();
  const [deletingTask, setDeletingTask] = useState<TaskSummary>();
  const [error, setError] = useState<string>();
  const needs = attention.data?.length ?? 0;

  async function act(work: () => Promise<unknown>) {
    setError(undefined);
    try {
      await work();
      refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const actions: TaskActions = {
    onPick,
    groups: groups.data ?? [],
    move: (task, groupId) => void act(() => api.moveTask(task.id, groupId)),
    newGroupFor: (task) => {
      setName("");
      setNaming({ task });
    },
    remove: (task) => setDeletingTask(task),
    pin: (task, on) =>
      void act(() => (on ? api.pinTask(task.id) : api.unpinTask(task.id))),
  };

  async function saveName() {
    const target = naming;
    if (!target || !name.trim()) return;
    setNaming(undefined);
    await act(async () => {
      if (target.group) return api.renameTaskGroup(target.group.id, name);
      const group = await api.createTaskGroup(name);
      if (target.task) await api.moveTask(target.task.id, group.id);
    });
  }

  if (collapsed) {
    return (
      <div className="flex h-full flex-col items-center gap-2 px-2 py-3">
        <Logo collapsed />
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          icon={PanelLeftOpen}
          aria-label={t("nav.expand")}
          onClick={onToggle}
        />
        <Link
          to="/"
          aria-label={t("tasks.new")}
          className="flex h-9 w-9 items-center justify-center rounded-full text-muted hover:bg-hovered hover:text-fg"
        >
          <Ico icon={Plus} size={18} />
        </Link>
        <Link
          to="/schedules"
          aria-label={t("schedule.section")}
          className="flex h-9 w-9 items-center justify-center rounded-full text-muted hover:bg-hovered hover:text-fg"
        >
          <Ico icon={CalendarClock} size={18} />
        </Link>
        <Link
          to="/customize"
          aria-label={t("customize.title")}
          className="flex h-9 w-9 items-center justify-center rounded-full text-muted hover:bg-hovered hover:text-fg"
        >
          <Ico icon={Puzzle} size={18} />
        </Link>
        <Link
          to="/devices"
          aria-label={t("nav.devices")}
          className="flex h-9 w-9 items-center justify-center rounded-full text-muted hover:bg-hovered hover:text-fg"
        >
          <Ico icon={Smartphone} size={18} />
        </Link>
        {needs > 0 && (
          <span className="rounded-full bg-amber-soft px-1.5 text-[12px] text-amber tnum">
            {needs}
          </span>
        )}
        <div className="mt-auto w-full">
          <AccountMenu collapsed />
        </div>
      </div>
    );
  }
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between px-3 pt-3 pb-2">
        <Logo />
        {onToggle && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            icon={PanelLeftClose}
            aria-label={t("nav.collapse")}
            onClick={onToggle}
          />
        )}
      </div>
      {/* New task and search share one row shape: the same height, a 20 px icon
          column and the text starting on the same line. */}
      <div className="space-y-0.5 px-2">
        <NavLink
          to="/"
          end
          onClick={onPick}
          className={({ isActive }) =>
            cn(
              "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[14px] font-medium text-fg pointer-coarse:min-h-11",
              isActive ? "bg-selected" : "hover:bg-hovered",
            )
          }
        >
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg">
            <Plus size={14} strokeWidth={2} aria-hidden />
          </span>
          {t("tasks.new")}
        </NavLink>
        <label className="flex h-9 cursor-text items-center gap-2.5 rounded-lg px-2.5 text-muted hover:bg-hovered focus-within:bg-selected pointer-coarse:min-h-11">
          <Lead>
            <Ico icon={Search} size={16} />
          </Lead>
          <span className="sr-only">{t("tasks.search")}</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("tasks.search")}
            className="min-w-0 flex-1 bg-transparent text-[14px] text-fg placeholder:text-muted focus:outline-none"
          />
        </label>
        {/* The schedules have a page of their own: here, the way to it and how
            many are on. */}
        <NavLink
          to="/schedules"
          onClick={onPick}
          className={({ isActive }) =>
            cn(
              "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[14px] text-muted pointer-coarse:min-h-11",
              isActive
                ? "bg-selected text-fg"
                : "hover:bg-hovered hover:text-fg",
            )
          }
        >
          <Lead>
            <Ico icon={CalendarClock} size={16} />
          </Lead>
          <span className="min-w-0 flex-1 truncate">
            {t("schedule.section")}
          </span>
          {activeSchedules > 0 && (
            <span
              className={cn(
                "text-[12px] tnum",
                waitingSchedules ? "text-amber" : "text-faint",
              )}
            >
              {activeSchedules}
            </span>
          )}
        </NavLink>
        {/* Skills and plugins: what the agent knows how to do, on a page of its
            own. */}
        <NavLink
          to="/customize"
          onClick={onPick}
          className={({ isActive }) =>
            cn(
              "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[14px] text-muted pointer-coarse:min-h-11",
              isActive
                ? "bg-selected text-fg"
                : "hover:bg-hovered hover:text-fg",
            )
          }
        >
          <Lead>
            <Ico icon={Puzzle} size={16} />
          </Lead>
          <span className="min-w-0 flex-1 truncate">
            {t("customize.title")}
          </span>
        </NavLink>
      </div>
      {/* Softened where it scrolls out of sight under the new task and search rows and
          above the account. */}
      <div className="relative mt-3 flex min-h-0 flex-1 flex-col">
        <Fade side="top" show={edges.above} />
        <Fade side="bottom" show={edges.below} />
        <nav
          ref={edges.ref}
          aria-label={t("nav.main")}
          className="scroll-quiet min-h-0 flex-1 overflow-y-auto px-2 pb-3"
        >
          {error && (
            <p role="alert" className="mb-2 px-2.5 text-[12.5px] text-red">
              {error}
            </p>
          )}
          {search ? (
            <Section id="results" title={t("sidebar.results")}>
              <TaskList
                filter={{ q: search }}
                pageSize={TASK_PAGE}
                empty={t("sidebar.noMatch")}
                actions={actions}
              />
            </Section>
          ) : (
            <>
              <Section
                id="devices"
                title={t("nav.devices")}
                extra={
                  <NavLink
                    to="/devices"
                    end
                    onClick={onPick}
                    className="rounded px-1 text-[12px] text-faint hover:text-fg"
                  >
                    {t("sidebar.manage")}
                  </NavLink>
                }
              >
                <DeviceList devices={devices.data ?? []} onPick={onPick} />
              </Section>
              <PinnedTasks actions={actions} />
              {(groups.data ?? []).map((group) => (
                <Section
                  key={group.id}
                  id={`group-${group.id}`}
                  title={group.name}
                  icon={Folder}
                  extra={
                    <Menu
                      label={t("groups.menu")}
                      minWidth={200}
                      entries={[
                        {
                          id: "rename",
                          label: t("groups.rename"),
                          icon: Pencil,
                          onSelect: () => {
                            setName(group.name);
                            setNaming({ group });
                          },
                        },
                        {
                          id: "delete",
                          label: t("groups.delete"),
                          icon: Trash2,
                          danger: true,
                          onSelect: () => setDeletingGroup(group),
                        },
                      ]}
                      trigger={({ ref, open, toggle, onKeyDown }) => (
                        <button
                          ref={ref}
                          type="button"
                          aria-haspopup="menu"
                          aria-expanded={open}
                          aria-label={t("groups.menuFor", { name: group.name })}
                          onClick={toggle}
                          onKeyDown={onKeyDown}
                          className="flex h-6 w-6 items-center justify-center rounded-full text-faint hover:bg-line hover:text-fg"
                        >
                          <MoreHorizontal
                            size={14}
                            strokeWidth={1.75}
                            aria-hidden
                          />
                        </button>
                      )}
                    />
                  }
                >
                  <TaskList
                    filter={{ group: group.id, pinned: "without" }}
                    pageSize={GROUP_PAGE}
                    empty={t("groups.empty")}
                    actions={actions}
                  />
                </Section>
              ))}
              <Section
                id="tasks"
                title={t("nav.tasks")}
                extra={
                  <span className="flex items-center gap-1">
                    {needs > 0 && (
                      <span className="rounded-full bg-amber-soft px-1.5 text-[11.5px] text-amber tnum">
                        {t("sidebar.needs", { count: needs })}
                      </span>
                    )}
                    <button
                      type="button"
                      aria-label={t("groups.new")}
                      title={t("groups.new")}
                      onClick={() => {
                        setName("");
                        setNaming({});
                      }}
                      className="flex h-6 w-6 items-center justify-center rounded-full text-faint hover:bg-line hover:text-fg"
                    >
                      <FolderPlus size={14} strokeWidth={1.75} aria-hidden />
                    </button>
                  </span>
                }
              >
                <TaskList
                  filter={{ group: "none", pinned: "without" }}
                  pageSize={TASK_PAGE}
                  empty={t("sidebar.noTasks")}
                  actions={actions}
                />
              </Section>
            </>
          )}
        </nav>
      </div>
      <div className="border-t border-line p-2">
        <AccountMenu />
      </div>

      <Dialog
        open={!!naming}
        onClose={() => setNaming(undefined)}
        title={t(naming?.group ? "groups.rename" : "groups.new")}
        footer={
          <>
            <Button onClick={() => setNaming(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={!name.trim()}
              onClick={() => void saveName()}
            >
              {t(naming?.group ? "groups.save" : "groups.create")}
            </Button>
          </>
        }
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void saveName();
          }}
        >
          <Field htmlFor="group-name" label={t("groups.name")}>
            <input
              id="group-name"
              data-autofocus
              value={name}
              maxLength={80}
              onChange={(e) => setName(e.target.value)}
              className={inputClass}
            />
          </Field>
        </form>
      </Dialog>
      <Dialog
        open={!!deletingGroup}
        onClose={() => setDeletingGroup(undefined)}
        tone="red"
        title={t("groups.delete.title", { name: deletingGroup?.name ?? "" })}
        footer={
          <>
            <Button data-autofocus onClick={() => setDeletingGroup(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const group = deletingGroup;
                setDeletingGroup(undefined);
                if (group) void act(() => api.deleteTaskGroup(group.id));
              }}
            >
              {t("groups.delete")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{t("groups.delete.body")}</p>
      </Dialog>
      <Dialog
        open={!!deletingTask}
        onClose={() => setDeletingTask(undefined)}
        tone="red"
        title={t("detail.delete.title")}
        footer={
          <>
            <Button data-autofocus onClick={() => setDeletingTask(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const task = deletingTask;
                setDeletingTask(undefined);
                if (!task) return;
                void act(async () => {
                  await api.deleteTask(task.id);
                  if (location.pathname === `/tasks/${task.id}`)
                    navigate("/", { replace: true });
                });
              }}
            >
              {t("detail.delete")}
            </Button>
          </>
        }
      >
        <p className="mb-2 font-medium text-fg">{deletingTask?.request}</p>
        <DeleteBody />
      </Dialog>
    </div>
  );
}

function DeleteBody() {
  const { t } = useI18n();
  const { me } = useAuth();
  return (
    <p className="text-fg">
      {t("detail.delete.body", { days: me?.audit_retention_days ?? 0 })}
    </p>
  );
}

export function AccountMenu({ collapsed }: { collapsed?: boolean }) {
  const { t, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const { me, signOut } = useAuth();
  const navigate = useNavigate();
  const mark = (on: boolean) => (on ? Check : undefined);
  const entries: MenuEntry[] = [
    { id: "h-theme", header: t("account.theme") },
    {
      id: "light",
      label: t("account.theme.light"),
      icon: mark(theme === "light") ?? Sun,
      onSelect: () => setTheme("light"),
    },
    {
      id: "dark",
      label: t("account.theme.dark"),
      icon: mark(theme === "dark") ?? Moon,
      onSelect: () => setTheme("dark"),
    },
    {
      id: "system",
      label: t("account.theme.system"),
      icon: mark(theme === "system") ?? Monitor,
      onSelect: () => setTheme("system"),
    },
    { id: "s1", separator: true },
    { id: "h-lang", header: t("account.language") },
    ...LOCALES.map((l) => ({
      id: l.id,
      label: l.label,
      icon: mark(locale === l.id) ?? Languages,
      onSelect: () => setLocale(l.id),
    })),
    { id: "s2", separator: true },
    {
      id: "settings",
      label: t("settings.title"),
      icon: SettingsIcon,
      onSelect: () => navigate("/settings"),
    },
    {
      id: "out",
      label: t("account.signOut"),
      icon: LogOut,
      onSelect: () => {
        void signOut().then(() => navigate("/sign-in"));
      },
    },
  ];
  return (
    <Menu
      label={t("account.menu")}
      entries={entries}
      placement="top-start"
      minWidth={240}
      trigger={({ ref, open, toggle, onKeyDown }) => (
        <button
          ref={ref}
          type="button"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={t("account.menu")}
          onClick={toggle}
          onKeyDown={onKeyDown}
          className={cn(
            "flex w-full items-center gap-2.5 rounded-lg p-1.5 text-left hover:bg-hovered aria-expanded:bg-selected",
            collapsed && "justify-center",
          )}
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[13px] font-semibold text-accent">
            {me?.display_name.slice(0, 1).toUpperCase()}
          </span>
          {!collapsed && (
            <>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[14px] font-medium">
                  {me?.display_name}
                </span>
                <span className="block truncate text-[12px] text-muted">
                  {me?.tenant_name} ·{" "}
                  {t(
                    me?.role === "owner"
                      ? "account.role.owner"
                      : "account.role.member",
                  )}
                </span>
              </span>
              <ChevronsUpDown
                size={15}
                strokeWidth={1.5}
                className="text-faint"
              />
            </>
          )}
        </button>
      )}
    />
  );
}

/** What the sidebar's task menus ask of it: confirmations live in one place. */
export interface TaskActions {
  onPick?: () => void;
  groups: TaskGroupView[];
  move: (task: TaskSummary, groupId: string | null) => void;
  newGroupFor: (task: TaskSummary) => void;
  remove: (task: TaskSummary) => void;
  pin: (task: TaskSummary, on: boolean) => void;
}

/** The tasks the signed-in person pinned, latest pinned first, above the
 * groups; nothing while there are none. Pinned tasks are not listed again below. */
function PinnedTasks({ actions }: { actions: TaskActions }) {
  const { t } = useI18n();
  const list = useTaskList({ pinned: "only" }, PINNED_PAGE);
  const pinned = (list.data ?? []).filter((task) => task.pinned);
  if (!pinned.length) return null;
  return (
    <Section id="pinned" title={t("pins.title")} icon={Pin}>
      <ul className="space-y-px">
        {pinned.map((task) => (
          <TaskRow key={task.id} task={task} actions={actions} />
        ))}
      </ul>
    </Section>
  );
}
