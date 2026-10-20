import type { DeviceView } from "@/api/types.gen";
import type { MessageKey, Translate } from "@/i18n";

/** A task's agent can use a device that is online with nothing missing (the cloud's
 * `rules.usable`): a phone that is plugged in but does not answer reports a problem. */
export function usable(device: DeviceView): boolean {
  return device.online && device.problems.length === 0;
}

/** A person can take a device over when it is online and nothing keeps it from it but a
 * locked or dark screen, which the takeover tries waking (the cloud's
 * `rules.require_usable(takeover=True)`). */
export function takeoverable(device: DeviceView): boolean {
  return device.online && device.problems.every((p) => p === "locked");
}

/** A device can take a task when an owner enabled it and it is usable. */
export function ready(device: DeviceView): boolean {
  return device.enabled && usable(device);
}

/** Why a device cannot take a task now, or undefined when it can. */
export function unavailable(
  device: DeviceView,
  t: Translate,
): string | undefined {
  if (!device.enabled) return t("reason.not_enabled");
  if (!device.online || device.problems.includes("disconnected"))
    return t("reason.disconnected");
  if (device.problems.length)
    return t(`fix.${device.problems[0]}` as MessageKey);
  return undefined;
}

/** In a word or two, for a status chip, why a task's agent cannot use the device now;
 * undefined when it is `usable`. `unavailable` says what to do about it. */
export function unusableStatus(
  device: DeviceView,
  t: Translate,
): string | undefined {
  if (!device.online || device.problems.includes("disconnected"))
    return t("reason.disconnected");
  if (device.problems.includes("not_responding"))
    return t("reason.not_responding");
  if (device.problems.includes("locked")) return t("reason.locked");
  if (device.problems.length) return t("devices.status.attention");
  return undefined;
}

/** Devices in the order to offer them: ready ones first, then those at work for a
 * task or taken over, then those with a problem, then the ones not enabled; by name
 * within each. */
export function readyFirst(devices: DeviceView[]): DeviceView[] {
  const rank = (d: DeviceView) =>
    !d.enabled ? 3 : !usable(d) ? 2 : d.task_id || d.taken_over ? 1 : 0;
  return [...devices].sort(
    (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name),
  );
}

/** Devices grouped by the Gateway (pool) they are connected to, pools in name order. */
export function byPool(devices: DeviceView[]): [string, DeviceView[]][] {
  const pools = new Map<string, DeviceView[]>();
  for (const device of devices) {
    pools.set(device.pool, [...(pools.get(device.pool) ?? []), device]);
  }
  return [...pools.entries()].sort(([a], [b]) => a.localeCompare(b));
}
