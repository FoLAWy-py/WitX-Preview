import type { DeviceView, RunView, TaskDetail } from "@/api/types.gen";
import type { MessageKey, Translate } from "@/i18n";
import { usable } from "./pool";

/** The phones a run waits for because they cannot be used now (the cloud's
 * `rules.device_wait`); none when it waits for something else, such as a phone another
 * task holds. */
export function awayDevices(run: RunView, devices: DeviceView[]): DeviceView[] {
  if (run.status !== "waiting" || run.wait_reason !== "device") return [];
  return devices.filter((d) => run.device_ids.includes(d.id) && !usable(d));
}

/** What an agent is doing, in a few words. Waiting for a device is either for a phone
 * that cannot be used now, named, or for one that another task holds. */
export function runLabel(
  run: RunView,
  devices: DeviceView[],
  t: Translate,
): string {
  if (run.status !== "waiting" || !run.wait_reason)
    return t(`run.${run.status}` as MessageKey);
  const away = awayDevices(run, devices);
  if (away.length)
    return t("wait.reconnect", {
      device: away.map((d) => d.name).join(", "),
    });
  return t(`wait.${run.wait_reason}` as MessageKey);
}

/** Whether a running task is held up by its phones: its main agent waits for phones that
 * cannot be used now. The task itself is still running; only what it shows changes. */
export function waitsForPhone(
  task: TaskDetail,
  devices: DeviceView[],
): boolean {
  const root = task.runs.find((r) => !r.parent_run_id);
  return (
    task.status === "active" && !!root && awayDevices(root, devices).length > 0
  );
}

/** What the person calls an agent of the task: the main one; a sub-agent by the name
 * it was given, else by the phone it works on, else by its place among them. */
export function agentName(
  run: RunView,
  runs: RunView[],
  devices: DeviceView[],
  t: Translate,
): string {
  if (!run.parent_run_id) return t("panel.mainAgent");
  if (run.name) return run.name;
  const phone = devices.find((d) => d.id === run.device_ids[0]);
  if (phone) return t("agents.onPhone", { device: phone.name });
  const index = runs.filter((r) => r.parent_run_id).indexOf(run);
  return t("agents.numbered", { n: index + 1 });
}

const UUID =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;

/** An agent's text with the IDs of the workspace's phones shown as their names. */
export function nameDevices(text: string, devices: DeviceView[]): string {
  return text.replace(UUID, (id) => {
    const phone = devices.find((d) => d.id.toLowerCase() === id.toLowerCase());
    return phone ? phone.name : id;
  });
}
