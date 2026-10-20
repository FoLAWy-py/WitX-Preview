import { useState } from "react";
import type { ScheduleCreate, ScheduleView } from "@/api/types.gen";
import { useI18n, type Locale, type MessageKey, type Translate } from "@/i18n";
import { clock, cn, exactDate } from "@/lib/format";
import { CalendarClock } from "lucide-react";
import { useAuth } from "@/lib/auth";
import type { Proposal } from "@/lib/timeline";
import { useWorkspace } from "@/lib/workspace";
import { Button, Field, inputClass } from "./ui/base";
import { Dialog } from "./ui/dialog";
import { Select } from "./ui/select";

export type Repeat = ScheduleCreate["repeat"];

/** When a task starts by itself. */
export interface ScheduleSpec {
  repeat: Repeat;
  time_of_day: string; // "HH:MM", local to `timezone`
  timezone: string;
  weekdays: number[]; // 0 = Monday
  month_day: number | null;
  run_on: string | null; // "YYYY-MM-DD"
}

const REPEATS: Repeat[] = ["once", "daily", "weekdays", "weekly", "monthly"];
/** Monday first, as the server counts them. */
const WEEKDAY_KEYS: MessageKey[] = [
  "schedule.day.mon",
  "schedule.day.tue",
  "schedule.day.wed",
  "schedule.day.thu",
  "schedule.day.fri",
  "schedule.day.sat",
  "schedule.day.sun",
];

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function defaultSchedule(): ScheduleSpec {
  return {
    repeat: "daily",
    time_of_day: "09:00",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    weekdays: [0],
    month_day: 1,
    run_on: today(),
  };
}

/** "Weekdays at 08:30", "Every Mon, Wed at 09:00", "On 20 Oct at 10:00"… */
export function scheduleSummary(
  s: Pick<ScheduleSpec, "repeat" | "weekdays" | "month_day" | "run_on"> & {
    time_of_day: string;
  },
  t: Translate,
  locale: string,
): string {
  const time = s.time_of_day.slice(0, 5);
  switch (s.repeat) {
    case "once":
      return t("schedule.summary.once", {
        date: s.run_on
          ? new Date(`${s.run_on}T00:00`).toLocaleDateString(locale, {
              month: "short",
              day: "numeric",
            })
          : "",
        time,
      });
    case "daily":
      return t("schedule.summary.daily", { time });
    case "weekdays":
      return t("schedule.summary.weekdays", { time });
    case "weekly":
      return t("schedule.summary.weekly", {
        days: s.weekdays.map((d) => t(WEEKDAY_KEYS[d])).join(", "),
        time,
      });
    case "monthly":
      return t("schedule.summary.monthly", {
        day: String(s.month_day ?? 1),
        time,
      });
  }
}

/** What happened the last time a schedule was due, in words. */
export function scheduleOutcome(
  view: ScheduleView,
  t: Translate,
  locale: Locale,
): string | undefined {
  const outcome = view.last_outcome;
  if (!outcome) return undefined;
  if (outcome === "proposed") return t("schedule.outcome.proposed");
  if (outcome === "started")
    return view.last_run_at
      ? t("schedule.outcome.started", {
          time: exactDate(view.last_run_at, locale),
        })
      : undefined;
  const [what, why] = outcome.split(":");
  return t(
    what === "waiting"
      ? "schedule.outcome.waiting"
      : "schedule.outcome.skipped",
    {
      reason: t(`schedule.reason.${why}` as MessageKey),
    },
  );
}

/** When a schedule runs: how often, on which day or date, at what time, in its zone. */
export function ScheduleWhen({
  spec,
  onChange,
}: {
  spec: ScheduleSpec;
  onChange: (patch: Partial<ScheduleSpec>) => void;
}) {
  const { t } = useI18n();
  const set = onChange;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium">
            {t("schedule.repeat")}
          </span>
          <Select
            label={t("schedule.repeat")}
            value={spec.repeat}
            onChange={(repeat) => set({ repeat })}
            options={REPEATS.map((r) => ({
              value: r,
              label: t(`schedule.repeat.${r}` as MessageKey),
            }))}
          />
        </div>
        {spec.repeat === "once" && (
          <Field htmlFor="schedule-date" label={t("schedule.date")}>
            <input
              id="schedule-date"
              type="date"
              min={today()}
              value={spec.run_on ?? ""}
              onChange={(e) => set({ run_on: e.target.value || null })}
              className={cn(inputClass, "w-auto")}
            />
          </Field>
        )}
        {spec.repeat === "monthly" && (
          <Field htmlFor="schedule-day" label={t("schedule.monthDay")}>
            <input
              id="schedule-day"
              type="number"
              min={1}
              max={31}
              value={spec.month_day ?? 1}
              onChange={(e) =>
                set({
                  month_day: Math.min(
                    31,
                    Math.max(1, Number(e.target.value) || 1),
                  ),
                })
              }
              className={cn(inputClass, "w-24")}
            />
          </Field>
        )}
        <Field htmlFor="schedule-time" label={t("schedule.time")}>
          <input
            id="schedule-time"
            type="time"
            value={spec.time_of_day}
            onChange={(e) => set({ time_of_day: e.target.value })}
            className={cn(inputClass, "w-auto")}
          />
        </Field>
      </div>
      {spec.repeat === "weekly" && (
        <fieldset>
          <legend className="mb-1.5 text-[13px] font-medium">
            {t("schedule.days")}
          </legend>
          <div className="flex flex-wrap gap-1.5">
            {WEEKDAY_KEYS.map((key, day) => {
              const on = spec.weekdays.includes(day);
              return (
                <button
                  key={key}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    set({
                      weekdays: on
                        ? spec.weekdays.filter((d) => d !== day)
                        : [...spec.weekdays, day].sort(),
                    })
                  }
                  className={cn(
                    "h-9 min-w-11 rounded-lg border px-2.5 text-[13px] pointer-coarse:h-11",
                    on
                      ? "border-accent bg-accent-soft font-medium text-accent"
                      : "border-line text-muted hover:bg-sunken",
                  )}
                >
                  {t(key)}
                </button>
              );
            })}
          </div>
        </fieldset>
      )}
      <p className="text-[12.5px] text-muted">
        {t("schedule.timezone", { zone: spec.timezone })}{" "}
        {t("schedule.busyNote")}
      </p>
    </div>
  );
}

/** Whether a schedule's timing is complete enough to save. */
export function whenValid(spec: ScheduleSpec): boolean {
  return (
    /^\d{2}:\d{2}$/.test(spec.time_of_day) &&
    (spec.repeat !== "weekly" || spec.weekdays.length > 0) &&
    (spec.repeat !== "once" || !!spec.run_on) &&
    (spec.repeat !== "monthly" || (spec.month_day ?? 0) >= 1)
  );
}

/** A schedule's timing as the form holds it. */
export function specOf(s: ScheduleView): ScheduleSpec {
  return {
    repeat: s.repeat,
    time_of_day: s.time_of_day.slice(0, 5),
    timezone: s.timezone,
    weekdays: s.weekdays,
    month_day: s.month_day,
    run_on: s.run_on,
  };
}

export function ScheduleDialog({
  open,
  initial,
  onClose,
  onSave,
}: {
  open: boolean;
  initial: ScheduleSpec;
  onClose: () => void;
  onSave: (spec: ScheduleSpec) => void;
}) {
  const { t } = useI18n();
  const [spec, setSpec] = useState(initial);
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t("schedule.title")}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            variant="primary"
            disabled={!whenValid(spec)}
            onClick={() => onSave(spec)}
          >
            {t("schedule.set")}
          </Button>
        </>
      }
    >
      <ScheduleWhen
        spec={spec}
        onChange={(patch) => setSpec((s) => ({ ...s, ...patch }))}
      />
    </Dialog>
  );
}

/** A schedule the agent proposed (in the task's time zone): turned on or discarded. */
export function ProposalCard({ proposal }: { proposal: Proposal }) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const { schedules } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const current = schedules.data?.find((s) => s.id === proposal.schedule_id);
  const pending = current?.last_outcome === "proposed" && !current.enabled;

  async function act(work: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await work();
      await schedules.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-accent/30 bg-accent-soft/40 px-4 py-3">
      <div className="mb-1 flex items-center gap-1.5 text-[12.5px] font-medium text-accent">
        <CalendarClock size={14} aria-hidden />
        {t("schedule.proposal.title")}
      </div>
      <p className="text-[15px] break-words">{proposal.goal}</p>
      <p className="mt-0.5 text-[13px] text-muted">
        {scheduleSummary(proposal, t, locale)}
      </p>
      {!schedules.data ? null : !current ? (
        <p className="mt-2 text-[13px] text-faint">
          {t("schedule.proposal.discarded")}
        </p>
      ) : pending ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            busy={busy}
            onClick={() =>
              void act(() => api.setScheduleEnabled(current.id, true))
            }
          >
            {t("schedule.proposal.turnOn")}
          </Button>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => void act(() => api.deleteSchedule(current.id))}
          >
            {t("schedule.proposal.discard")}
          </Button>
          <span className="text-[12px] text-muted">
            {t("schedule.timezone", { zone: current.timezone })}
          </span>
        </div>
      ) : (
        <p className="mt-2 text-[13px] text-green">
          {current.enabled && current.next_run_at
            ? t("schedule.proposal.on", {
                when: `${exactDate(current.next_run_at, locale)} ${clock(current.next_run_at, locale)}`,
              })
            : t("schedule.paused")}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </div>
  );
}
