import type { ScheduleView } from "@/api/types.gen";

/**
 * Where a schedule stands, read from what the server keeps:
 * - active: on and due again (`enabled && next_run_at`);
 * - paused: turned off, or proposed by the agent and not turned on yet;
 * - completed: on but never due again: a one-time schedule that already ran.
 */
export type ScheduleState = "active" | "paused" | "completed";

export function scheduleState(s: ScheduleView): ScheduleState {
  if (!s.enabled) return "paused";
  return s.next_run_at ? "active" : "completed";
}

const time = (iso: string | null) => (iso ? new Date(iso).getTime() : 0);

/** The schedules page's groups: active ones soonest first, completed ones latest run first,
 * paused ones as the server lists them. */
export function groupSchedules(
  list: ScheduleView[],
): Record<ScheduleState, ScheduleView[]> {
  const groups: Record<ScheduleState, ScheduleView[]> = {
    active: [],
    paused: [],
    completed: [],
  };
  for (const s of list) groups[scheduleState(s)].push(s);
  groups.active.sort((a, b) => time(a.next_run_at) - time(b.next_run_at));
  groups.completed.sort((a, b) => time(b.last_run_at) - time(a.last_run_at));
  return groups;
}

/** What became of a schedule's latest moment, for the line under the message box. */
export type ScheduleNotice = "scheduled" | "started" | "waiting" | "skipped";

export function scheduleNotice(s: ScheduleView): ScheduleNotice {
  const outcome = s.last_outcome ?? "";
  if (outcome === "started" && s.last_task_id) return "started";
  if (outcome.startsWith("waiting:")) return "waiting";
  if (outcome.startsWith("skipped:")) return "skipped";
  return "scheduled";
}

/** How often, and how many times, the schedules reload once a new one is due: the
 * worker starts it within about a second, so the line under the message box follows
 * at once instead of at the next 30 s refresh. */
export const DUE_RELOAD_MS = 2_000;
export const DUE_RELOADS = 15;
