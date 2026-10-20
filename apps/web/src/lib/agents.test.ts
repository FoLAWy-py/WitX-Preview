import { describe, expect, it } from "vitest";
import type { DeviceView, RunView, TaskDetail } from "@/api/types.gen";
import { agentName, nameDevices, runLabel, waitsForPhone } from "./agents";

const t = ((key: string, vars?: Record<string, unknown>) =>
  `${key}${vars ? JSON.stringify(vars) : ""}`) as never;
const run = (over: Partial<RunView>) =>
  ({
    id: "r",
    parent_run_id: "root",
    device_ids: [],
    name: null,
    ...over,
  }) as RunView;
const phone = {
  id: "2f969d84-da41-4a64-863f-9b3ca4a2ba7c",
  name: "iPhone 18 Pro",
} as DeviceView;

describe("agents", () => {
  it("names a sub-agent by its given name, else its phone, else its place", () => {
    expect(agentName(run({ parent_run_id: null }), [], [], t)).toBe(
      "panel.mainAgent",
    );
    expect(agentName(run({ name: "iOS Agent" }), [], [phone], t)).toBe(
      "iOS Agent",
    );
    expect(agentName(run({ device_ids: [phone.id] }), [], [phone], t)).toBe(
      'agents.onPhone{"device":"iPhone 18 Pro"}',
    );
    const second = run({ id: "b" });
    expect(agentName(second, [run({ id: "a" }), second], [], t)).toBe(
      'agents.numbered{"n":2}',
    );
  });

  it("shows a phone's ID in an agent's text as the phone's name", () => {
    expect(
      nameDevices(
        "第二台设备（2f969d84-da41-4a64-863f-9b3ca4a2ba7c）和 00000000-0000-0000-0000-000000000000",
        [phone],
      ),
    ).toBe(
      "第二台设备（iPhone 18 Pro）和 00000000-0000-0000-0000-000000000000",
    );
  });

  it("says which phone a run waits for when it cannot be used, else that it is busy", () => {
    const away = {
      ...phone,
      online: true,
      problems: ["not_responding"],
    } as DeviceView;
    const waiting = run({
      status: "waiting",
      wait_reason: "device",
      device_ids: [phone.id],
    });
    expect(runLabel(waiting, [away], t)).toBe(
      'wait.reconnect{"device":"iPhone 18 Pro"}',
    );
    const there = { ...away, problems: [] } as DeviceView;
    expect(runLabel(waiting, [there], t)).toBe("wait.device");
    expect(runLabel(run({ status: "running" }), [away], t)).toBe("run.running");
  });

  it("says a running task waits for its phone while its main agent waits for one it cannot use", () => {
    const away = {
      ...phone,
      online: true,
      problems: ["not_responding"],
    } as DeviceView;
    const main = run({
      parent_run_id: null,
      status: "waiting",
      wait_reason: "device",
      device_ids: [phone.id],
    });
    const task = (over: Partial<TaskDetail>) =>
      ({ status: "active", runs: [main], ...over }) as TaskDetail;
    expect(waitsForPhone(task({}), [away])).toBe(true);
    // A phone another task holds is not a phone that cannot be used.
    expect(waitsForPhone(task({}), [{ ...away, problems: [] }])).toBe(false);
    expect(waitsForPhone(task({ status: "paused" }), [away])).toBe(false);
    // A sub-agent waiting for its phone does not hold the whole task up.
    const sub = { ...main, id: "s", parent_run_id: "root" };
    const working = {
      ...main,
      status: "running",
      wait_reason: null,
    } as RunView;
    expect(waitsForPhone(task({ runs: [working, sub] }), [away])).toBe(false);
  });
});
