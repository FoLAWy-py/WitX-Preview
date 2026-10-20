/** A schedule as the server lists it: active (on and due again) unless patched. */
export function scheduleView(id: string, goal: string, patch: object = {}) {
  return {
    id,
    goal,
    constraints: [],
    device_ids: ["d1"],
    permission_mode: "auto",
    model: "gpt-6-luna",
    reasoning_effort: "medium",
    repeat: "daily",
    time_of_day: "08:00:00",
    timezone: "Asia/Shanghai",
    weekdays: [],
    month_day: null,
    run_on: null,
    enabled: true,
    next_run_at: new Date(Date.now() + 3_600_000).toISOString(),
    last_run_at: null,
    last_task_id: null,
    last_outcome: null,
    ...patch,
  };
}
