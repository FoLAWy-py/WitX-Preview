import { describe, expect, it } from "vitest";
import type { DeviceView } from "@/api/types.gen";
import en from "@/i18n/en";
import zh from "@/i18n/zh";
import {
  byPool,
  ready,
  readyFirst,
  takeoverable,
  unusableStatus,
  usable,
} from "./pool";

const device = (over: Partial<DeviceView>): DeviceView => ({
  id: "d",
  name: "Pixel",
  platform: "android",
  online: true,
  capabilities: ["tap"],
  task_id: null,
  task_goal: null,
  pool: "Desk Mac",
  problems: [],
  enabled: true,
  ...over,
});

// Every problem a Gateway can report; the type makes this list complete.
const PROBLEMS: Record<DeviceView["problems"][number], true> = {
  disconnected: true,
  tool_missing: true,
  adb_unauthorized: true,
  adb_offline: true,
  not_paired: true,
  developer_mode_off: true,
  appium_unreachable: true,
  driver_missing: true,
  wda_unsigned: true,
  identity_changed: true,
  not_responding: true,
  locked: true,
};

describe("device pool", () => {
  it("offers only enabled, online devices with nothing missing for a task", () => {
    expect(ready(device({}))).toBe(true);
    expect(ready(device({ enabled: false }))).toBe(false);
    expect(ready(device({ online: false }))).toBe(false);
    expect(ready(device({ problems: ["adb_unauthorized"] }))).toBe(false);
  });

  it("groups devices by the Gateway they are connected to", () => {
    const pools = byPool([
      device({ id: "1", pool: "Office" }),
      device({ id: "2", pool: "Desk Mac" }),
      device({ id: "3", pool: "Office" }),
    ]);
    expect(
      pools.map(([pool, members]) => [pool, members.map((d) => d.id)]),
    ).toEqual([
      ["Desk Mac", ["2"]],
      ["Office", ["1", "3"]],
    ]);
  });

  it("says in a word why a task cannot use a phone, and nothing for one it can", () => {
    const t = ((key: string) => key) as never;
    expect(unusableStatus(device({ problems: ["not_responding"] }), t)).toBe(
      "reason.not_responding",
    );
    expect(unusableStatus(device({ online: false }), t)).toBe(
      "reason.disconnected",
    );
    expect(unusableStatus(device({ problems: ["adb_offline"] }), t)).toBe(
      "devices.status.attention",
    );
    expect(unusableStatus(device({}), t)).toBeUndefined();
  });

  it("says how to fix every problem a Gateway can report, in both languages", () => {
    for (const problem of Object.keys(PROBLEMS)) {
      const key = `fix.${problem}` as keyof typeof en;
      expect(en[key], problem).toBeTruthy();
      expect(zh[key], problem).toBeTruthy();
    }
  });
});

describe("a locked phone", () => {
  it("is not usable for a task but can be taken over, and says it is locked", () => {
    const phone = device({ problems: ["locked"] });
    expect(usable(phone)).toBe(false);
    expect(takeoverable(phone)).toBe(true);
    expect(
      takeoverable(device({ problems: ["locked", "not_responding"] })),
    ).toBe(false);
    expect(takeoverable(device({ online: false, problems: ["locked"] }))).toBe(
      false,
    );
    expect(unusableStatus(phone, (key: string) => key) as never).toBe(
      "reason.locked",
    );
  });
  it("offers ready devices first, then those at work, then those with a problem, then the ones not enabled", () => {
    const names = readyFirst([
      device({ name: "off", enabled: false }),
      device({ name: "locked", problems: ["locked"] }),
      device({ name: "busy", task_id: "t1" }),
      device({ name: "b ready" }),
      device({ name: "a ready" }),
      device({ name: "taken", taken_over: true }),
    ]).map((d) => d.name);
    expect(names).toEqual([
      "a ready",
      "b ready",
      "busy",
      "taken",
      "locked",
      "off",
    ]);
  });
});
