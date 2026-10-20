import { useState } from "react";
import { useNavigate } from "react-router";
import {
  CalendarCheck,
  CalendarClock,
  CirclePause,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  Plus,
  Smartphone,
  SquareArrowOutUpRight,
  Trash2,
  type LucideIcon,
} from "lucide-react";
import type { ScheduleView } from "@/api/types.gen";
import { useI18n, type MessageKey } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { effortOn, effortsOf } from "@/lib/efforts";
import { cn, moment } from "@/lib/format";
import {
  groupSchedules,
  scheduleNotice,
  scheduleState,
  type ScheduleState,
} from "@/lib/schedules";
import { usePageTitle } from "@/lib/title";
import { useWorkspace } from "@/lib/workspace";
import { PageBar } from "@/components/Shell";
import {
  deviceOptions,
  ModelSelect,
  ModeSelect,
  type Effort,
} from "@/components/Composer";
import {
  ScheduleWhen,
  scheduleOutcome,
  scheduleSummary,
  specOf,
  whenValid,
  type ScheduleSpec,
} from "@/components/Schedule";
import {
  Button,
  EmptyState,
  ErrorState,
  Ico,
  inputClass,
  Spinner,
} from "@/components/ui/base";
import { Dialog } from "@/components/ui/dialog";
import { Menu, type MenuEntry } from "@/components/ui/popover";
import { MultiSelect } from "@/components/ui/select";

const stateIcon: Record<ScheduleState, LucideIcon> = {
  active: CalendarClock,
  paused: CirclePause,
  completed: CalendarCheck,
};

/** What a schedule does and when, changed in one place: its words, its phones,
 * how it may act, its model, and its timing. It stays on or off as it was. */
function ScheduleEditor({
  schedule: s,
  onClose,
  onSaved,
}: {
  schedule: ScheduleView;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const { devices, models } = useWorkspace();
  const [goal, setGoal] = useState(s.goal);
  const [phones, setPhones] = useState<string[]>(s.device_ids);
  const [mode, setMode] = useState(s.permission_mode);
  const [model, setModel] = useState(s.model);
  const [chosenEffort, setEffort] = useState<Effort>(s.reasoning_effort);
  // One the model does not go down to: that model's lowest instead.
  const effort = effortOn(effortsOf(models, model), chosenEffort);
  const [when, setWhen] = useState<ScheduleSpec>(() => specOf(s));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const all = devices.data ?? [];
  const names = phones
    .map((id) => all.find((d) => d.id === id)?.name)
    .filter(Boolean);
  const save = async () => {
    setBusy(true);
    setError(undefined);
    try {
      await api.editSchedule(s.id, {
        goal: goal.trim(),
        constraints: s.constraints,
        device_ids: phones,
        permission_mode: mode,
        model,
        reasoning_effort: effort,
        ...when,
      });
      onSaved();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };
  return (
    <Dialog
      open
      wide
      onClose={onClose}
      title={t("schedules.edit.title")}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            variant="primary"
            disabled={busy || !goal.trim() || !whenValid(when)}
            onClick={() => void save()}
          >
            {t("schedules.edit.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <label className="block space-y-1.5">
          <span className="text-[13px] font-medium">
            {t("schedules.edit.goal")}
          </span>
          <textarea
            value={goal}
            rows={3}
            maxLength={4000}
            onChange={(e) => setGoal(e.target.value)}
            className={cn(inputClass, "h-auto resize-y py-2 leading-relaxed")}
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <MultiSelect
            label={t("composer.devices")}
            icon={Smartphone}
            values={phones}
            summary={
              names.length ? names.join(", ") : t("composer.devices.empty")
            }
            options={deviceOptions(all, t)}
            onChange={setPhones}
            empty={t("composer.devices.empty")}
          />
          <ModeSelect value={mode} onChange={setMode} />
          <ModelSelect
            model={model}
            effort={effort}
            onModel={setModel}
            onEffort={setEffort}
          />
        </div>
        <ScheduleWhen
          spec={when}
          onChange={(patch) => setWhen((w) => ({ ...w, ...patch }))}
        />
        {error && (
          <p role="alert" className="text-[13px] text-red">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}

/** One schedule: what it does, when it runs and last ran, on which phones, and what can
 * be done with it. */
function ScheduleCard({
  schedule: s,
  onEdit,
  act,
}: {
  schedule: ScheduleView;
  onEdit: () => void;
  act: (work: () => Promise<unknown>) => void;
}) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const { devices } = useWorkspace();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);
  const state = scheduleState(s);
  const proposed = s.last_outcome === "proposed";
  const waiting = state === "active" && scheduleNotice(s) === "waiting";
  const phones = s.device_ids
    .map((id) => devices.data?.find((d) => d.id === id)?.name)
    .filter(Boolean)
    .join(", ");
  const when =
    state === "active" && s.next_run_at
      ? t("schedule.next", { when: moment(s.next_run_at, locale) })
      : state === "paused" && !proposed
        ? t("schedule.paused")
        : undefined;
  const outcome = scheduleOutcome(s, t, locale);
  const entries: MenuEntry[] = [
    ...(s.last_task_id
      ? [
          {
            id: "open",
            label: t("schedules.openLast"),
            icon: SquareArrowOutUpRight,
            onSelect: () => navigate(`/tasks/${s.last_task_id}`),
          },
          { id: "s", separator: true } as MenuEntry,
        ]
      : []),
    {
      id: "delete",
      label: t("schedule.delete"),
      icon: Trash2,
      danger: true,
      hint: t("schedule.delete.hint"),
      onSelect: () => setDeleting(true),
    },
  ];
  return (
    <li className="flex items-start gap-3 rounded-xl border border-line bg-surface p-4">
      <Ico
        icon={stateIcon[state]}
        size={18}
        className={cn("mt-0.5", state === "active" ? "text-fg" : "text-faint")}
      />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="line-clamp-2 text-[14.5px] font-medium break-words">
          {s.goal}
        </p>
        <p className="text-[13px] text-muted">
          {scheduleSummary(s, t, locale)}
          {when && <> · {when}</>}
        </p>
        {outcome && (
          <p
            className={cn("text-[13px]", waiting ? "text-amber" : "text-faint")}
          >
            {outcome}
          </p>
        )}
        <p className="text-[12.5px] text-faint">
          {[
            phones || t("composer.devices.empty"),
            t(`mode.${s.permission_mode}` as MessageKey),
            `${s.model} · ${t(`effort.${s.reasoning_effort}` as MessageKey)}`,
          ].join(" · ")}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button size="sm" icon={Pencil} onClick={onEdit}>
          {t("schedules.edit")}
        </Button>
        {s.enabled ? (
          <Button
            size="sm"
            variant="ghost"
            icon={Pause}
            // A completed schedule has nothing left to pause.
            disabled={state === "completed"}
            onClick={() => act(() => api.setScheduleEnabled(s.id, false))}
          >
            {t("schedule.pause")}
          </Button>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            icon={Play}
            onClick={() => act(() => api.setScheduleEnabled(s.id, true))}
          >
            {t(proposed ? "schedule.proposal.turnOn" : "schedule.resume")}
          </Button>
        )}
        <Menu
          label={t("schedule.menuFor", { goal: s.goal })}
          entries={entries}
          minWidth={210}
          placement="bottom-end"
          trigger={({ ref, open, toggle, onKeyDown }) => (
            <Button
              ref={ref}
              size="sm"
              variant="ghost"
              iconOnly
              icon={MoreHorizontal}
              aria-haspopup="menu"
              aria-expanded={open}
              aria-label={t("schedule.menuFor", { goal: s.goal })}
              onClick={toggle}
              onKeyDown={onKeyDown}
            />
          )}
        />
      </div>
      <Dialog
        open={deleting}
        onClose={() => setDeleting(false)}
        tone="red"
        title={t("schedules.delete.title")}
        footer={
          <>
            <Button onClick={() => setDeleting(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setDeleting(false);
                act(() => api.deleteSchedule(s.id));
              }}
            >
              {t("schedule.delete")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{s.goal}</p>
        <p className="mt-2 text-[13px] text-muted">
          {t("schedule.delete.hint")}
        </p>
      </Dialog>
    </li>
  );
}

/** Tasks that start by themselves, on a page of their own: active ones
 * soonest first, then paused and proposed ones, then completed one-time ones. */
export default function Schedules() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { schedules } = useWorkspace();
  const [editing, setEditing] = useState<ScheduleView>();
  const [error, setError] = useState<string>();
  usePageTitle(t("schedule.section"));
  const groups = groupSchedules(schedules.data ?? []);
  const act = (work: () => Promise<unknown>) => {
    setError(undefined);
    work().then(
      () => schedules.reload(),
      (err: Error) => setError(err.message),
    );
  };
  const create = () => navigate("/?schedule=1");
  const sections: [ScheduleState, MessageKey][] = [
    ["active", "schedules.active"],
    ["paused", "schedule.paused"],
    ["completed", "schedule.completed"],
  ];
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <PageBar>
        <h1 className="flex-1 text-[15px] font-semibold">
          {t("schedule.section")}
        </h1>
        <Button size="sm" variant="primary" icon={Plus} onClick={create}>
          {t("schedule.new")}
        </Button>
      </PageBar>
      <main className="scroll-quiet flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[920px] space-y-6 px-4 py-6">
          {schedules.error && (
            <ErrorState
              message={t("schedules.error")}
              onRetry={() => void schedules.reload()}
            />
          )}
          {error && (
            <p role="alert" className="text-[13px] text-red">
              {error}
            </p>
          )}
          {!schedules.data && !schedules.error && <Spinner />}
          {schedules.data?.length === 0 && (
            <EmptyState
              art={
                <Ico icon={CalendarClock} size={28} className="text-faint" />
              }
              title={t("schedule.none")}
              body={t("schedules.empty")}
              action={
                <Button variant="primary" icon={Plus} onClick={create}>
                  {t("schedule.new")}
                </Button>
              }
            />
          )}
          {sections.map(([state, title]) =>
            groups[state].length ? (
              <section key={state} aria-label={t(title)}>
                <h2 className="mb-2 text-[13px] text-muted">
                  {t(title)}{" "}
                  <span className="text-faint">· {groups[state].length}</span>
                </h2>
                <ul className="space-y-2">
                  {groups[state].map((s) => (
                    <ScheduleCard
                      key={s.id}
                      schedule={s}
                      onEdit={() => setEditing(s)}
                      act={act}
                    />
                  ))}
                </ul>
              </section>
            ) : null,
          )}
        </div>
      </main>
      {editing && (
        <ScheduleEditor
          schedule={editing}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined);
            void schedules.reload();
          }}
        />
      )}
    </div>
  );
}
