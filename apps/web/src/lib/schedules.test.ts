import { describe, expect, it } from "vitest";
import type { ScheduleView } from "@/api/types.gen";
import { groupSchedules, scheduleNotice, scheduleState } from "./schedules";

function schedule(patch: Partial<ScheduleView>): ScheduleView {
  return {
    id: "s",
    goal: "Open Notes",
    constraints: [],
    device_ids: [],
    permission_mode: "auto",
    model: "m",
    reasoning_effort: "medium",
    repeat: "daily",
    time_of_day: "08:00:00",
    timezone: "UTC",
    weekdays: [],
    month_day: null,
    run_on: null,
    enabled: true,
    next_run_at: "2026-10-17T08:00:00Z",
    last_run_at: null,
    last_task_id: null,
    last_outcome: null,
    ...patch,
  };
}

describe("schedule states", () => {
  it("a schedule that is on and due again is active", () => {
    expect(scheduleState(schedule({}))).toBe("active");
  });

  it("a turned-off or proposed schedule is paused", () => {
    expect(scheduleState(schedule({ enabled: false, next_run_at: null }))).toBe(
      "paused",
    );
    expect(
      scheduleState(
        schedule({
          enabled: false,
          next_run_at: null,
          last_outcome: "proposed",
        }),
      ),
    ).toBe("paused");
  });

  it("a one-time schedule that already ran is completed", () => {
    expect(
      scheduleState(
        schedule({
          repeat: "once",
          next_run_at: null,
          last_outcome: "started",
          last_task_id: "t1",
        }),
      ),
    ).toBe("completed");
  });

  it("active schedules are listed soonest first, completed ones latest first", () => {
    const groups = groupSchedules([
      schedule({ id: "later", next_run_at: "2026-10-18T08:00:00Z" }),
      schedule({
        id: "old",
        next_run_at: null,
        last_run_at: "2026-10-01T08:00:00Z",
      }),
      schedule({ id: "soon", next_run_at: "2026-10-16T08:00:00Z" }),
      schedule({ id: "off", enabled: false, next_run_at: null }),
      schedule({
        id: "new",
        next_run_at: null,
        last_run_at: "2026-10-15T08:00:00Z",
      }),
    ]);
    expect(groups.active.map((s) => s.id)).toEqual(["soon", "later"]);
    expect(groups.paused.map((s) => s.id)).toEqual(["off"]);
    expect(groups.completed.map((s) => s.id)).toEqual(["new", "old"]);
  });
});

describe("the line under the message box", () => {
  it("follows the schedule's latest moment", () => {
    expect(scheduleNotice(schedule({}))).toBe("scheduled");
    expect(
      scheduleNotice(schedule({ last_outcome: "started", last_task_id: "t1" })),
    ).toBe("started");
    expect(
      scheduleNotice(schedule({ last_outcome: "waiting:device_busy" })),
    ).toBe("waiting");
    expect(
      scheduleNotice(schedule({ last_outcome: "skipped:device_offline" })),
    ).toBe("skipped");
  });
});
