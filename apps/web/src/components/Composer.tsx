import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import {
  ArrowUp,
  ArrowUpRight,
  Brain,
  CalendarClock,
  Cpu,
  ClipboardList,
  Eraser,
  Folder,
  FolderPlus,
  ListChecks,
  Lock,
  MessagesSquare,
  OctagonX,
  Pause,
  Pencil,
  Play,
  Plus,
  Puzzle,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Smartphone,
  Sparkle,
  Sparkles,
  Square,
  Zap,
  Target,
  X,
  type LucideIcon,
} from "lucide-react";
import type { DeviceView, ScheduleView, TaskDetail } from "@/api/types.gen";
import { effortOn, effortsOf, shownModel, type Effort } from "@/lib/efforts";
import { useI18n, type MessageKey, type Translate } from "@/i18n";
import { useAuth } from "@/lib/auth";
import {
  draftKey,
  lastChoice,
  rememberChoice,
  useStoredText,
} from "@/lib/stored";
import { clock, cn, exactDate } from "@/lib/format";
import { unavailable } from "@/lib/pool";
import { DUE_RELOADS, DUE_RELOAD_MS, scheduleNotice } from "@/lib/schedules";
import { useWorkspace } from "@/lib/workspace";
import {
  ScheduleDialog,
  defaultSchedule,
  scheduleOutcome,
  scheduleSummary,
  type ScheduleSpec,
} from "./Schedule";
import { awaitingAnswer, mainAsks } from "./Conversation";
import { useSlashMenu, type SlashCommand } from "./SlashMenu";
import { useSkillCommands } from "./Skills";
import { Button, Field, Ico, Segmented, inputClass } from "./ui/base";
import { Dialog } from "./ui/dialog";
import { Menu, type Placement } from "./ui/popover";
import {
  MultiSelect,
  Select,
  triggerClass,
  type SelectOption,
} from "./ui/select";

export type Mode = TaskDetail["permission_mode"];
export type { Effort };
export const modeIcon: Record<Mode, LucideIcon> = {
  ask: ShieldQuestion,
  auto: ShieldCheck,
  full: ShieldAlert,
};

export function deviceOptions(
  devices: DeviceView[],
  t: Translate,
): SelectOption<string>[] {
  return [...devices]
    .sort(
      (a, b) => a.pool.localeCompare(b.pool) || a.name.localeCompare(b.name),
    )
    .map((d) => ({
      value: d.id,
      label: d.name,
      group: d.pool,
      disabledReason: unavailable(d, t),
      // A busy phone can still be chosen: the task waits until it is free.
      description: d.task_id || d.taken_over ? t("reason.busy") : undefined,
    }));
}

/** Permission mode as a dropdown; full access needs the owner's leave and a confirmation. */
export function ModeSelect({
  value,
  onChange,
  disabled,
  placement,
}: {
  value: Mode;
  onChange: (mode: Mode) => void;
  disabled?: boolean;
  placement?: Placement;
}) {
  const { t } = useI18n();
  const { me } = useAuth();
  const [confirm, setConfirm] = useState(false);
  const allowed =
    me?.role === "owner" || !!me?.allow_full_access || value === "full";
  const options: SelectOption<Mode>[] = (["ask", "auto", "full"] as Mode[]).map(
    (m) => ({
      value: m,
      label: t(`mode.${m}` as MessageKey),
      description: t(`mode.${m}.help` as MessageKey),
      icon: m === "full" && !allowed ? Lock : modeIcon[m],
      disabledReason:
        m === "full" && !allowed ? t("mode.full.locked") : undefined,
      danger: m === "full",
    }),
  );
  return (
    <>
      <Select
        label={t("composer.mode")}
        value={value}
        options={options}
        disabled={disabled}
        onChange={(m) => (m === "full" ? setConfirm(true) : onChange(m))}
        placement={placement}
        minWidth={300}
      />
      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        tone="red"
        title={t("mode.full.confirm.title")}
        footer={
          <>
            <Button data-autofocus onClick={() => setConfirm(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setConfirm(false);
                onChange("full");
              }}
            >
              {t("mode.full.confirm.ok")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{t("mode.full.confirm.body")}</p>
      </Dialog>
    </>
  );
}

/** How the effort reads on the trigger: low and medium stay quiet, each step up a little
 * stronger, the top two in the accent with a spark. Plain type and color, no effects. */
const effortLook: Record<Effort, { icon?: LucideIcon; className: string }> = {
  low: { className: "text-muted" },
  medium: { className: "text-muted" },
  high: { className: "text-fg" },
  xhigh: { icon: Sparkle, className: "text-accent" },
  max: { icon: Sparkles, className: "font-semibold text-accent" },
};

/** The model and how hard it thinks, in one menu: the trigger reads "gpt-6-luna · High";
 * the effort is chosen below the models, from those the shown model offers (medium by
 * default; some models go down to low, #287). */
export function ModelSelect({
  model,
  effort,
  onModel,
  onEffort,
  disabled,
  placement,
}: {
  model: string | undefined;
  effort: Effort;
  onModel: (model: string) => void;
  onEffort: (effort: Effort) => void;
  disabled?: boolean;
  placement?: Placement;
}) {
  const { t } = useI18n();
  const { models } = useWorkspace();
  if (!models.length) return null;
  const current = shownModel(models, model) ?? models[0].name;
  const efforts = effortsOf(models, current);
  return (
    <Select
      label={t("composer.model")}
      value={current}
      icon={Cpu}
      disabled={disabled}
      placement={placement}
      // Five efforts need a little more room than four for their names on one line.
      minWidth={efforts.length > 4 ? 340 : 300}
      suffix={{
        text: t(`effort.${effort}` as MessageKey),
        ...effortLook[effort],
      }}
      stayOpen
      options={models.map((m) => ({
        value: m.name,
        label: m.name,
        description: m.default
          ? `${m.provider} · ${t("composer.model.default")}`
          : m.provider,
        disabledReason: m.allowed
          ? undefined
          : t("composer.model.notAllowed", { provider: m.provider }),
      }))}
      onChange={onModel}
      footer={
        <div className="px-2.5 pt-1.5 pb-2">
          <div className="mb-1.5 flex items-center gap-1.5 text-[12.5px] font-medium text-muted">
            <Brain size={14} aria-hidden />
            {t("composer.effort")}
          </div>
          <Segmented
            label={t("composer.effort")}
            value={effort}
            onChange={onEffort}
            className="flex w-full flex-nowrap [&>button]:flex-1 [&>button]:justify-center [&>button]:px-1"
            // Each effort looks in the list as it will on the trigger.
            options={efforts.map((e) => ({
              value: e,
              label: t(`effort.${e}` as MessageKey),
              icon: effortLook[e].icon,
              tone: e === "medium" ? undefined : effortLook[e].className,
            }))}
          />
          <p className="mt-1.5 text-[12.5px] text-muted">
            {t(`effort.${effort}.help` as MessageKey)}
          </p>
        </div>
      }
    />
  );
}

/** Which group a new task is filed in, asked before it starts: none by default, any
 * group there is, or a new one named here. Shown once there are groups; a task can
 * still be moved later from the sidebar. */
function GroupSelect({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (groupId: string | undefined) => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const { groups, refresh } = useWorkspace();
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const list = groups.data ?? [];
  const options: SelectOption<string>[] = [
    { value: "none", label: t("groups.noGroup"), icon: Folder },
    ...list.map((g) => ({ value: g.id, label: g.name, icon: Folder })),
    // Last, like an action: choosing it names a new group and picks it.
    { value: "new", label: t("groups.newEllipsis"), icon: FolderPlus },
  ];
  async function create() {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const group = await api.createTaskGroup(name.trim());
      refresh();
      onChange(group.id);
      setNaming(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Select
        label={t("groups.pick")}
        value={value && list.some((g) => g.id === value) ? value : "none"}
        icon={Folder}
        // No group: just the folder, so the row of choices stays on one line.
        iconOnly={!value || !list.some((g) => g.id === value)}
        options={options}
        onChange={(v) => {
          if (v !== "new") return onChange(v === "none" ? undefined : v);
          setName("");
          setError(undefined);
          setNaming(true);
        }}
        placement="top-start"
      />
      <Dialog
        open={naming}
        onClose={() => setNaming(false)}
        title={t("groups.new")}
        footer={
          <>
            <Button onClick={() => setNaming(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={!name.trim()}
              busy={busy}
              onClick={() => void create()}
            >
              {t("groups.create")}
            </Button>
          </>
        }
      >
        <div
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void create();
            }
          }}
        >
          <Field htmlFor="new-task-group" label={t("groups.name")}>
            <input
              id="new-task-group"
              data-autofocus
              value={name}
              maxLength={80}
              onChange={(e) => setName(e.target.value)}
              className={inputClass}
            />
          </Field>
          {error && (
            <p role="alert" className="mt-2 text-[13px] text-red">
              {error}
            </p>
          )}
        </div>
      </Dialog>
    </>
  );
}

/** The new-task box on the home page. */
export function NewTaskComposer({
  initialGoal = "",
  openSchedule = false,
}: {
  initialGoal?: string;
  /** Opens the schedule dialog at once (the sidebar's "New schedule"). */
  openSchedule?: boolean;
}) {
  const { t, locale } = useI18n();
  const { api, me } = useAuth();
  const { devices, groups, models, refresh, schedules } = useWorkspace();
  const navigate = useNavigate();
  // What is being written stays when the page is left or reloaded.
  const [goal, setGoal] = useStoredText(draftKey(me?.user_id, "new"));
  const [constraints, setConstraints] = useStoredText(
    draftKey(me?.user_id, "new.constraints"),
  );
  const [showConstraints, setShowConstraints] = useState(() => !!constraints);
  // A suggestion picked on the home page replaces the draft.
  useEffect(() => {
    if (initialGoal) setGoal(initialGoal);
  }, [initialGoal, setGoal]);
  // A new task starts with the settings the person last chose.
  const [last] = useState(() => lastChoice(me?.user_id));
  const fullAllowed = me?.role === "owner" || !!me?.allow_full_access;
  const [picked, setPicked] = useState<string[]>([]);
  const [mode, setMode] = useState<Mode>(() =>
    last.mode === "full" && !fullAllowed ? "auto" : (last.mode ?? "auto"),
  );
  const [chosenModel, setModel] = useState<string | undefined>(last.model);
  // One no longer offered, or not allowed here any more: the default instead.
  const model = models.some((m) => m.name === chosenModel && m.allowed)
    ? chosenModel
    : undefined;
  const [chosenEffort, setEffort] = useState<Effort>(last.effort ?? "medium");
  // One the model does not go down to: that model's lowest instead.
  const effort = effortOn(
    effortsOf(models, shownModel(models, model)),
    chosenEffort,
  );
  const [group, setGroup] = useState<string>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [schedule, setSchedule] = useState<ScheduleSpec>();
  const [scheduling, setScheduling] = useState(openSchedule);
  const [scheduled, setScheduled] = useState<ScheduleView>();
  const goalRef = useRef<HTMLTextAreaElement>(null);
  const all = devices.data ?? [];
  const usable = all.filter((d) => !unavailable(d, t));
  // A single usable phone is picked for you; otherwise you choose.
  const chosen = picked.length
    ? picked.filter((id) => usable.some((d) => d.id === id))
    : usable.length === 1
      ? [usable[0].id]
      : [];
  const summary =
    chosen.length === 0
      ? t(usable.length ? "composer.devices.choose" : "composer.devices.none")
      : chosen.length === 1
        ? (all.find((d) => d.id === chosen[0])?.name ?? t("common.device"))
        : t("composer.devices.selected", { count: chosen.length });

  // The first message is the request; "/" offers what the "+" menu holds, and `/goal`
  // starts the task with a goal; "$" names a skill to use.
  const skillCommands = useSkillCommands();
  const slash = useSlashMenu(
    goal,
    [
      {
        name: "/goal",
        hint: t("command.goalStart"),
        icon: Target,
        fill: "/goal ",
      },
      {
        name: "/plan",
        hint: t("command.plan"),
        icon: ClipboardList,
        fill: "/plan ",
      },
      {
        name: "/skill",
        hint: t("command.skillStart"),
        icon: Puzzle,
        fill: "/skill ",
      },
      {
        name: "/schedule",
        hint: t("schedule.add"),
        icon: CalendarClock,
        run: () => setScheduling(true),
      },
      {
        name: "/constraints",
        hint: t("composer.addConstraints"),
        icon: ListChecks,
        run: () => setShowConstraints(true),
      },
      ...skillCommands,
    ],
    setGoal,
  );

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!goal.trim()) {
      setError(t("composer.needGoal"));
      goalRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(undefined);
    setScheduled(undefined);
    const request = {
      goal: goal.trim(),
      constraints: constraints
        .split("\n")
        .map((c) => c.trim())
        .filter(Boolean),
      device_ids: chosen,
      model,
      permission_mode: mode,
      reasoning_effort: effort,
      // "Tomorrow" and the schedules the agent proposes are in this time zone.
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    };
    try {
      if (schedule) {
        // It starts by itself later: say when, and keep the person here.
        const created = await api.createSchedule({ ...request, ...schedule });
        await schedules.reload();
        setScheduled(created);
        setGoal("");
        setConstraints("");
        setSchedule(undefined);
        setBusy(false);
        return;
      }
      // A group deleted meanwhile is no longer offered: the task goes in none.
      const filed = groups.data?.some((g) => g.id === group);
      const task = await api.createTask(
        filed ? { ...request, group_id: group } : request,
      );
      // Sent: the draft is done with.
      setGoal("");
      setConstraints("");
      refresh();
      navigate(`/tasks/${task.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      aria-label={t("composer.title")}
      className="@container rounded-2xl border border-line-strong bg-surface p-2 shadow-[0_1px_2px_rgb(0_0_0/0.04)] focus-within:border-accent/60"
    >
      {slash.menu}
      <label htmlFor="goal" className="sr-only">
        {t("composer.goal")}
      </label>
      <textarea
        id="goal"
        ref={goalRef}
        rows={1}
        autoFocus
        value={goal}
        onChange={(e) => {
          setGoal(e.target.value);
          setError(undefined);
        }}
        placeholder={t("composer.goal.placeholder")}
        aria-autocomplete="list"
        onKeyDown={(e) => {
          if (slash.onKeyDown(e)) return;
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void submit();
          }
        }}
        className="block max-h-[40vh] min-h-[52px] w-full resize-none bg-transparent px-3 py-2.5 text-[16px] leading-relaxed placeholder:text-faint focus:outline-none [field-sizing:content]"
      />
      {showConstraints && (
        <div className="px-3 pb-2">
          <label
            htmlFor="constraints"
            className="mb-1 block text-[12.5px] font-medium text-muted"
          >
            {t("composer.constraints")}
          </label>
          <textarea
            id="constraints"
            rows={2}
            value={constraints}
            onChange={(e) => setConstraints(e.target.value)}
            placeholder={t("composer.constraints.placeholder")}
            className="block w-full resize-none rounded-lg border border-line bg-sunken px-3 py-2 text-[14px] placeholder:text-faint focus:border-accent focus:outline-none"
          />
        </div>
      )}
      <div className="flex items-center gap-1.5 px-1 pt-1">
        <div className="scroll-row flex min-w-0 flex-1 items-center gap-1.5">
          {/* Less used than the phones, mode and model: one "+" away. */}
          <Menu
            label={t("composer.more")}
            placement="top-start"
            minWidth={220}
            entries={[
              {
                id: "schedule",
                label: t(schedule ? "schedule.edit" : "schedule.add"),
                icon: CalendarClock,
                onSelect: () => setScheduling(true),
              },
              {
                id: "constraints",
                label: t("composer.addConstraints"),
                icon: ListChecks,
                disabled: showConstraints,
                onSelect: () => setShowConstraints(true),
              },
            ]}
            trigger={({ ref, open, toggle, onKeyDown }) => (
              <Button
                ref={ref}
                variant="ghost"
                iconOnly
                icon={Plus}
                aria-label={t("composer.more")}
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={toggle}
                onKeyDown={onKeyDown}
                className="rounded-full"
              />
            )}
          />
          <MultiSelect
            label={t("composer.devices")}
            icon={Smartphone}
            values={chosen}
            summary={summary}
            options={deviceOptions(all, t)}
            onChange={setPicked}
            empty={t("composer.devices.empty")}
            placement="top-start"
          />
          <ModeSelect
            value={mode}
            onChange={(m) => {
              setMode(m);
              rememberChoice(me?.user_id, { mode: m });
            }}
            placement="top-start"
          />
          <ModelSelect
            model={model}
            effort={effort}
            onModel={(m) => {
              setModel(m);
              rememberChoice(me?.user_id, { model: m });
            }}
            onEffort={(e) => {
              setEffort(e);
              rememberChoice(me?.user_id, { effort: e });
            }}
            placement="top-start"
          />
          {/* A schedule's tasks are not filed in a group: only a task started now asks. */}
          {!schedule && !!groups.data?.length && (
            <GroupSelect value={group} onChange={setGroup} />
          )}
          {/* A chosen schedule stays in sight: what will happen, and a way to undo it. */}
          {schedule && (
            <span className="inline-flex items-center">
              <button
                type="button"
                onClick={() => setScheduling(true)}
                aria-label={t("schedule.change", {
                  when: scheduleSummary(schedule, t, locale),
                })}
                className={cn(
                  triggerClass,
                  "border-accent/50 bg-accent-soft text-accent",
                )}
              >
                <Ico icon={CalendarClock} size={15} />
                <span>{scheduleSummary(schedule, t, locale)}</span>
              </button>
              <Button
                variant="ghost"
                size="sm"
                iconOnly
                icon={X}
                aria-label={t("schedule.clear")}
                onClick={() => setSchedule(undefined)}
              />
            </span>
          )}
        </div>
        <button
          type="submit"
          disabled={busy || !goal.trim()}
          aria-label={t(schedule ? "schedule.submit" : "composer.start")}
          className="press flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-fg text-bg disabled:opacity-25 pointer-coarse:h-11 pointer-coarse:w-11"
        >
          <ArrowUp size={18} strokeWidth={2} aria-hidden />
        </button>
      </div>
      {chosen.length === 0 && usable.length === 0 && all.length > 0 && (
        <p className="px-3 pt-2 text-[12.5px] text-muted">
          {t("composer.devices.noneReady")}
        </p>
      )}
      {error && (
        <p role="alert" className="px-3 pt-2 text-[13px] text-red">
          {error}
        </p>
      )}
      {scheduled && <ScheduledNotice created={scheduled} />}
      {scheduling && (
        <ScheduleDialog
          open
          initial={schedule ?? defaultSchedule()}
          onClose={() => setScheduling(false)}
          onSave={(spec) => {
            setSchedule(spec);
            setScheduling(false);
          }}
        />
      )}
    </form>
  );
}

/**
 * The line under the message box after a schedule is made here. It follows the
 * schedule: when it first runs, then the task it started (a link), or why it waits or
 * was skipped. From its first moment the schedules reload every couple of seconds until
 * something happens, rather than at the workspace's 30 s refresh.
 */
function ScheduledNotice({ created }: { created: ScheduleView }) {
  const { t, locale } = useI18n();
  const { schedules } = useWorkspace();
  const { reload } = schedules;
  // Deleted meanwhile: nothing left to say.
  const current = schedules.data
    ? schedules.data.find((s) => s.id === created.id)
    : created;
  const notice = current ? scheduleNotice(current) : undefined;
  const first = created.next_run_at;
  const pending = notice === "scheduled" && !!current?.enabled;
  useEffect(() => {
    if (!first || !pending) return;
    let left = DUE_RELOADS;
    let timer = 0;
    const tick = () => {
      void reload();
      if (--left > 0) timer = window.setTimeout(tick, DUE_RELOAD_MS);
    };
    timer = window.setTimeout(
      tick,
      Math.max(0, new Date(first).getTime() - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [first, pending, reload]);
  if (!current || !notice) return null;
  if (notice === "started" && current.last_task_id)
    return (
      <p
        role="status"
        className="flex min-w-0 items-center gap-1 px-3 pt-2 text-[13px] text-green"
      >
        <span className="shrink-0">{t("schedule.notice.started")}</span>
        <Link
          to={`/tasks/${current.last_task_id}`}
          className="group inline-flex min-w-0 items-center gap-0.5 text-fg hover:underline hover:underline-offset-2"
        >
          <span className="truncate">{current.goal}</span>
          <ArrowUpRight
            size={13}
            strokeWidth={1.75}
            aria-hidden
            className="shrink-0 text-faint group-hover:text-fg"
          />
        </Link>
      </p>
    );
  if (notice === "waiting" || notice === "skipped")
    return (
      <p role="status" className="px-3 pt-2 text-[13px] text-amber">
        {scheduleOutcome(current, t, locale)}
      </p>
    );
  return (
    <p role="status" className="px-3 pt-2 text-[13px] text-green">
      {t("schedule.created", {
        when: first
          ? `${exactDate(first, locale)} ${clock(first, locale)}`
          : "",
      })}
    </p>
  );
}

/**
 * The phones of a task that already started. Any ready phone can join; one already in
 * the task leaves only while the task is not running (no input can be under way).
 */
function TaskDevices({
  task,
  all,
  disabled,
  onChange,
}: {
  task: TaskDetail;
  all: DeviceView[];
  disabled?: boolean;
  onChange: (ids: string[]) => void;
}) {
  const { t } = useI18n();
  const running = ["active", "pausing", "stopping"].includes(task.status);
  const options = deviceOptions(all, t).map((o) =>
    task.device_ids.includes(o.value)
      ? {
          ...o,
          disabledReason: running
            ? t("composer.devices.pauseToRemove")
            : undefined,
        }
      : o,
  );
  const names = task.device_ids
    .map((id) => all.find((d) => d.id === id)?.name)
    .filter(Boolean);
  return (
    <MultiSelect
      label={t("composer.devices")}
      icon={Smartphone}
      values={task.device_ids}
      summary={
        names.length === 0
          ? t("composer.devices.none")
          : names.length === 1
            ? (names[0] as string)
            : t("composer.devices.selected", { count: names.length })
      }
      options={options}
      onChange={(ids) => !disabled && onChange(ids)}
      empty={t("composer.devices.empty")}
      placement="top-start"
    />
  );
}

/** What a task's page does for a command typed in its box. */
export type TaskCommand = "side" | "stop";

/**
 * The box at the bottom of a task, like a chat's: a message for the agent, the task's
 * permission mode and model, and one button that sends what you typed, or else pauses
 * a running task or resumes a paused one. "/" offers commands (`/goal`, a side chat,
 * pause, stop, …).
 */
export function TaskComposer({
  task,
  onChanged,
  onCommand,
  draft,
}: {
  task: TaskDetail;
  onChanged: () => Promise<void>;
  onCommand?: (command: TaskCommand) => void;
  /** A message's text to edit here (from the conversation); `seq` tells each apart. */
  draft?: { text: string; seq: number };
}) {
  const { t } = useI18n();
  const { api, me } = useAuth();
  const { devices } = useWorkspace();
  // What is being written to this task stays when the page is left or reloaded.
  const [text, setText] = useStoredText(draftKey(me?.user_id, task.id));
  const [drafted, setDrafted] = useState(draft?.seq);
  // What is already typed is kept: the message to edit comes after it.
  if (draft && draft.seq !== drafted) {
    setDrafted(draft.seq);
    setText((typed) =>
      typed.trim() ? `${typed}\n\n${draft.text}` : draft.text,
    );
  }
  useEffect(() => {
    if (!draft) return;
    const box = document.getElementById("note") as HTMLTextAreaElement | null;
    box?.focus();
    box?.setSelectionRange(box.value.length, box.value.length);
  }, [draft]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const stopping = task.status === "stopping";
  const finished = ["stopped", "completed", "failed"].includes(task.status);
  // The message box answers the main agent; a sub-agent's question has its own card.
  const asked = mainAsks(task);
  const subAsks = !asked && awaitingAnswer(task);
  const settingGoal = /^\/goal\s/i.test(text);
  // Messages the main agent has not read yet: shown here, not in the conversation,
  // until it reads them; they can be taken back to edit, removed, or sent now.
  const queued = task.inputs.filter((i) => i.queued);
  // Only one's own message can be taken back, and not a command: it took effect.
  const mine = (input: (typeof queued)[number]) =>
    input.created_by === me?.user_id && !/^\/(goal|plan)\b/i.test(input.text);
  // Sending now does something while the main agent works; while it waits for
  // its sub-agents the message already reached it in a side turn.
  const main = task.runs.find(
    (r) =>
      r.parent_run_id === null &&
      ["running", "runnable", "waiting"].includes(r.status),
  );
  const canSteer =
    task.status === "active" &&
    !!main &&
    !(
      main.status === "waiting" &&
      ["children", "user_input", "paused"].includes(main.wait_reason ?? "")
    );
  async function takeBack(seq: number, edit: boolean) {
    if (busy) return;
    const input = queued.find((i) => i.seq === seq);
    setBusy(true);
    setError(undefined);
    try {
      await api.withdrawInput(task.id, seq);
      // Back in the box at once: the text is not lost if the refresh fails.
      if (edit && input)
        setText((current) =>
          current ? `${input.text}\n${current}` : input.text,
        );
      await onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const placeholder = asked
    ? t("note.placeholder.answer")
    : finished
      ? t("note.placeholder.finished")
      : ["pausing", "paused"].includes(task.status)
        ? t("note.placeholder.paused")
        : t("note.placeholder");

  async function run(work: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    setError(undefined);
    try {
      await work();
      await onChanged();
      return true;
    } catch (err) {
      setError((err as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    const body = text.trim();
    if (!body || stopping) return;
    // Like a chat: writing to a paused task carries on with it.
    const sent = await run(() =>
      api.addInput(task.id, { text: body, resume: true }),
    );
    if (sent) setText("");
  }

  const live = ["active", "pausing", "paused"].includes(task.status);
  const commands: SlashCommand[] = [
    { name: "/goal", hint: t("command.goal"), icon: Target, fill: "/goal " },
    ...(task.goal
      ? [
          task.goal_paused
            ? {
                name: "/goal resume",
                hint: t("command.goalResume"),
                icon: Play,
                fill: "/goal resume",
              }
            : {
                name: "/goal pause",
                hint: t("command.goalPause"),
                icon: Pause,
                fill: "/goal pause",
              },
          {
            name: "/goal clear",
            hint: t("command.goalClear"),
            icon: Eraser,
            fill: "/goal clear",
          },
        ]
      : []),
    task.plan_mode
      ? {
          name: "/plan approve",
          hint: t("command.planApprove"),
          icon: Play,
          fill: "/plan approve",
        }
      : {
          name: "/plan",
          hint: t("command.plan"),
          icon: ClipboardList,
          fill: "/plan ",
        },
    ...(task.plan_mode
      ? [
          {
            name: "/plan off",
            hint: t("command.planOff"),
            icon: Eraser,
            fill: "/plan off",
          },
        ]
      : []),
    {
      name: "/skill",
      hint: t("command.skill"),
      icon: Puzzle,
      fill: "/skill",
    },
    ...(onCommand
      ? [
          {
            name: "/side",
            hint: t("command.side"),
            icon: MessagesSquare,
            run: () => onCommand("side"),
          },
        ]
      : []),
    ...(task.status === "active"
      ? [
          {
            name: "/pause",
            hint: t("detail.pause"),
            icon: Pause,
            run: () => void run(() => api.pause(task.id)),
          },
        ]
      : []),
    ...(task.status === "paused"
      ? [
          {
            name: "/resume",
            hint: t("detail.resume"),
            icon: Play,
            run: () => void run(() => api.resume(task.id)),
          },
        ]
      : []),
    ...(live && onCommand
      ? [
          {
            name: "/stop",
            hint: t("detail.stop"),
            icon: OctagonX,
            run: () => onCommand("stop"),
          },
        ]
      : []),
  ];
  const skillCommands = useSkillCommands();
  const slash = useSlashMenu(text, [...commands, ...skillCommands], setText);

  // The one button: send when there is text; otherwise pause or resume.
  const typed = !!text.trim();
  const button: {
    label: string;
    icon: LucideIcon;
    onClick?: () => void;
    spin?: boolean;
  } = typed
    ? { label: t("note.send"), icon: ArrowUp, onClick: () => void send() }
    : task.status === "active"
      ? {
          label: t("detail.pause"),
          icon: Square,
          onClick: () => void run(() => api.pause(task.id)),
        }
      : task.status === "paused"
        ? {
            label: t("detail.resume"),
            icon: Play,
            onClick: () => void run(() => api.resume(task.id)),
          }
        : task.status === "pausing" || stopping
          ? {
              label: t(stopping ? "status.stopping" : "status.pausing"),
              icon: Square,
              spin: true,
            }
          : { label: t("note.send"), icon: ArrowUp };
  const Icon = button.icon;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
      className={cn(
        "@container rounded-2xl border border-line-strong bg-surface p-2 shadow-[0_1px_2px_rgb(0_0_0/0.04)] focus-within:border-accent/60",
        asked && "border-amber ring-3 ring-amber/20",
        stopping && "opacity-60",
      )}
    >
      {queued.length > 0 && (
        <div
          role="group"
          aria-label={t("queue.label")}
          className="mb-1 space-y-1 border-b border-line px-3 pt-1 pb-2"
        >
          <p className="flex items-center gap-2 text-[12.5px] text-muted">
            <span className="min-w-0 flex-1">{t("queue.hint")}</span>
            {canSteer && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(() => api.steer(task.id))}
                className="flex shrink-0 cursor-pointer items-center gap-1 font-medium text-accent hover:underline disabled:opacity-50"
              >
                <Zap size={13} aria-hidden />
                {t("queue.now")}
              </button>
            )}
          </p>
          {queued.map((input) => (
            <div
              key={input.seq}
              className="group/q flex items-start gap-2 text-[14px] text-faint"
            >
              <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">
                {input.text}
              </span>
              {mine(input) && (
                <>
                  <button
                    type="button"
                    aria-label={t("queue.edit")}
                    title={t("queue.edit")}
                    disabled={busy}
                    onClick={() => void takeBack(input.seq, true)}
                    className="shrink-0 cursor-pointer p-0.5 text-faint hover:text-fg"
                  >
                    <Pencil size={13} aria-hidden />
                  </button>
                  <button
                    type="button"
                    aria-label={t("queue.remove")}
                    title={t("queue.remove")}
                    disabled={busy}
                    onClick={() => void takeBack(input.seq, false)}
                    className="shrink-0 cursor-pointer p-0.5 text-faint hover:text-fg"
                  >
                    <X size={13} aria-hidden />
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      {asked && (
        <p className="flex items-center gap-1.5 px-3 pt-1 text-[12.5px] font-medium text-amber">
          <span
            aria-hidden
            className="anim-pulse h-1.5 w-1.5 rounded-full bg-amber"
          />
          {t("question.waiting")}
        </p>
      )}
      {subAsks && (
        <p className="flex items-center gap-1.5 px-3 pt-1 text-[12.5px] text-muted">
          <span
            aria-hidden
            className="anim-pulse h-1.5 w-1.5 rounded-full bg-amber"
          />
          {t("question.subWaiting")}
        </p>
      )}
      {slash.menu}
      {task.plan_mode && (
        <p className="flex items-center gap-1.5 px-3 pt-1 text-[12.5px] font-medium text-accent">
          <ClipboardList size={14} aria-hidden />
          <span className="min-w-0 flex-1">{t("composer.planMode")}</span>
          <button
            type="button"
            className="shrink-0 text-muted underline-offset-2 hover:text-fg hover:underline"
            onClick={() =>
              void run(() => api.addInput(task.id, { text: "/plan off" }))
            }
          >
            {t("composer.planOff")}
          </button>
        </p>
      )}
      {settingGoal && (
        <p className="flex items-center gap-1.5 px-3 pt-1 text-[12.5px] font-medium text-accent">
          <Target size={14} aria-hidden />
          {t("command.goal.hint")}
        </p>
      )}
      <label htmlFor="note" className="sr-only">
        {t("note.label")}
      </label>
      <textarea
        id="note"
        rows={1}
        aria-autocomplete="list"
        value={text}
        disabled={stopping}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing || slash.onKeyDown(e)) return;
          // Like Claude Code: ↑ in an empty box takes the newest queued message back.
          const last = [...queued].reverse().find(mine);
          if (e.key === "ArrowUp" && !text && last) {
            e.preventDefault();
            if (!e.repeat) void takeBack(last.seq, true);
            return;
          }
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void send();
          }
        }}
        className="block max-h-[30vh] min-h-[44px] w-full resize-none bg-transparent px-3 py-2.5 text-[15px] leading-relaxed placeholder:text-faint focus:outline-none [field-sizing:content]"
      />
      <div className="flex items-center gap-1.5 px-1">
        <div className="scroll-row flex min-w-0 flex-1 items-center gap-1.5">
          <TaskDevices
            task={task}
            all={devices.data ?? []}
            disabled={stopping}
            onChange={(ids) => void run(() => api.setDevices(task.id, ids))}
          />
          {/* Changeable at any time, a finished task too: what is sent next, a retry
              or an edited message included, runs with them. */}
          <ModeSelect
            value={task.permission_mode}
            placement="top-start"
            onChange={(mode) => {
              rememberChoice(me?.user_id, { mode });
              void run(() =>
                api.setPermissionMode(task.id, { permission_mode: mode }),
              );
            }}
          />
          <ModelSelect
            model={task.model}
            effort={task.reasoning_effort}
            placement="top-start"
            onModel={(model) => {
              rememberChoice(me?.user_id, { model });
              void run(() => api.setModel(task.id, { model }));
            }}
            onEffort={(effort) => {
              rememberChoice(me?.user_id, { effort });
              void run(() => api.setReasoningEffort(task.id, effort));
            }}
          />
        </div>
        <button
          type="button"
          onClick={button.onClick}
          disabled={busy || !button.onClick}
          aria-label={button.label}
          title={button.label}
          className="press flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-fg text-bg disabled:opacity-25 pointer-coarse:h-11 pointer-coarse:w-11"
        >
          <Icon
            size={button.icon === Square ? 12 : 16}
            strokeWidth={2}
            fill={button.icon === ArrowUp ? "none" : "currentColor"}
            className={cn(button.spin && "anim-pulse")}
            aria-hidden
          />
        </button>
      </div>
      {error && (
        <p role="alert" className="px-3 pt-1.5 text-[12.5px] text-red">
          {error}
        </p>
      )}
    </form>
  );
}
