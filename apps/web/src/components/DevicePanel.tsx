import { useEffect, useId, useState } from "react";
import { Bot, CircleAlert, Hand, Radio, VideoOff } from "lucide-react";
import type {
  ActionView,
  DeviceView,
  LiveFrame,
  TaskDetail,
} from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { clock, cn, exactTime, moment, number } from "@/lib/format";
import { LIVE_GIVE_UP, liveDelay } from "@/lib/frame";
import { takeoverable, unavailable, unusableStatus, usable } from "@/lib/pool";
import { latestScreens } from "@/lib/timeline";
import { agentName, runLabel } from "@/lib/agents";
import { DeviceFrame } from "./DeviceFrame";
import { Button, Chip, Ico, Mono, PlatformIcon } from "./ui/base";

/** Every device the task's agents use, the main agent's first. */
export function taskDeviceIds(task: TaskDetail): string[] {
  const ids: string[] = [];
  for (const run of [...task.runs].sort(
    (a, b) => (a.parent_run_id ? 1 : 0) - (b.parent_run_id ? 1 : 0),
  )) {
    for (const id of run.device_ids) if (!ids.includes(id)) ids.push(id);
  }
  for (const o of task.observations)
    if (!ids.includes(o.device_id)) ids.push(o.device_id);
  return ids;
}

/** The input to draw on a device's latest screenshot: the one waiting, or the last sent. */
function markFor(task: TaskDetail, observationId: string): ActionView | null {
  return (
    task.actions.find(
      (a) =>
        a.observation_id === observationId && a.status === "awaiting_approval",
    ) ??
    [...task.actions]
      .reverse()
      .find((a) => a.post_observation_id === observationId) ??
    null
  );
}

/**
 * The device's screen, about once a second (the capture itself takes ~0.5 s), while this
 * panel is open and the page is visible. A picture the device was too busy to take is
 * skipped; the last one stays. `failing` says the last few asks in a row brought nothing:
 * then it asks less and less often (`liveDelay`), and every second again after a picture.
 */
function useLiveFrame(
  taskId: string,
  deviceId: string | undefined,
  on: boolean,
) {
  const { api } = useAuth();
  const [frame, setFrame] = useState<LiveFrame>();
  const [failing, setFailing] = useState(false);
  useEffect(() => {
    setFrame(undefined);
    setFailing(false);
    if (!on || !deviceId) return;
    let stopped = false;
    let misses = 0;
    let timer: ReturnType<typeof setTimeout>;
    const next = async () => {
      const started = Date.now();
      if (document.visibilityState === "visible") {
        try {
          const shot = await api.liveFrame(taskId, deviceId);
          misses = 0;
          if (!stopped) {
            setFrame(shot);
            setFailing(false);
          }
        } catch {
          // busy, or not held any more: keep what is shown
          misses += 1;
          if (!stopped && misses >= LIVE_GIVE_UP) setFailing(true);
        }
      }
      if (!stopped)
        timer = setTimeout(
          next,
          Math.max(0, liveDelay(misses) - (Date.now() - started)),
        );
    };
    void next();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [api, taskId, deviceId, on]);
  return { frame, failing };
}

/** The right-hand panel: the task's phones as they are now. */
export function DevicePanel({
  task,
  devices,
  onTakeOver,
  takingOver,
  onOpenAgent,
}: {
  task: TaskDetail;
  devices: DeviceView[];
  onTakeOver: (deviceId: string) => void;
  takingOver?: string;
  /** Opens a sub-agent's tab. */
  onOpenAgent?: (runId: string) => void;
}) {
  const { t, locale } = useI18n();
  const ids = taskDeviceIds(task);
  const [chosen, setActive] = useState<string>();
  const active = chosen && ids.includes(chosen) ? chosen : ids[0];
  const screens = latestScreens(task);
  const device = devices.find((d) => d.id === active);
  const why = device && unavailable(device, t);
  const obsId = active ? screens.get(active) : undefined;
  const obs = task.observations.find((o) => o.id === obsId);
  const mark = obsId ? markFor(task, obsId) : null;
  const whyId = useId();
  const canTakeOver = ["active", "paused"].includes(task.status);
  const held =
    !!device &&
    device.task_id === task.id &&
    ["active", "pausing", "paused"].includes(task.status);
  // A phone that is offline or does not respond sends no picture: there is nothing to
  // connect to, and the person is told why instead. The devices list refreshes on its
  // own, so watching starts again once the phone is back.
  const away = held && !usable(device) ? unusableStatus(device, t) : undefined;
  // Watch the screen live while the task holds the device, except while an input
  // waits for approval: then the screen it was chosen on (with the mark) matters.
  const awaiting = task.actions.some((a) => a.status === "awaiting_approval");
  const watching = held && !away && !awaiting;
  const { frame: live, failing } = useLiveFrame(task.id, active, watching);
  const shotAt = live?.captured_at ?? obs?.captured_at;

  return (
    <div className="flex flex-col gap-5 p-4">
      {/* The tab says what this is; only whether the picture is live is left to say.
          While it should be live the line keeps its place: "connecting" until the
          first picture of this phone comes (after switching phones too), then "live";
          "no live picture" while pictures do not come. A phone that cannot be used
          says why. */}
      {(watching || away) && (
        <div className="flex" aria-live="polite">
          {away ? (
            <Chip tone="amber" icon={CircleAlert}>
              {away}
            </Chip>
          ) : failing ? (
            <Chip tone="slate" icon={VideoOff}>
              {t("panel.noLive")}
            </Chip>
          ) : live ? (
            <Chip tone="accent" icon={Radio} pulse>
              {t("panel.live")}
            </Chip>
          ) : (
            <Chip icon={Radio}>{t("panel.connecting")}</Chip>
          )}
        </div>
      )}

      {ids.length === 0 && (
        <p className="text-[13.5px] text-muted">{t("panel.noDevice")}</p>
      )}

      {ids.length > 1 && (
        <div
          role="tablist"
          aria-label={t("panel.tab.phone")}
          className="flex gap-1 rounded-[10px] border border-line bg-sunken p-1"
        >
          {ids.map((id) => {
            const d = devices.find((x) => x.id === id);
            return (
              <button
                key={id}
                role="tab"
                aria-selected={id === active}
                onClick={() => setActive(id)}
                className={cn(
                  "flex h-8 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md px-2 text-[13px] font-medium pointer-coarse:min-h-11",
                  id === active
                    ? "bg-selected text-fg"
                    : "text-muted hover:bg-hovered hover:text-fg",
                )}
              >
                {d && <PlatformIcon platform={d.platform} size={14} />}
                <span className="truncate">
                  {d?.name ?? t("common.device")}
                </span>
              </button>
            );
          })}
        </div>
      )}

      {active && (
        <section className="flex flex-col items-center">
          {/* A phone held sideways uses the panel's width. */}
          <div
            className={cn(
              "w-full",
              (live?.width ?? obs?.width ?? 0) >
                (live?.height ?? obs?.height ?? 1)
                ? "max-w-none"
                : "max-w-[280px]",
            )}
          >
            <DeviceFrame
              platform={device?.platform ?? "ios"}
              obs={obs}
              live={live}
              mark={mark}
              label={t("panel.latest", {
                device: device?.name ?? t("common.device"),
              })}
              empty={t("panel.noShot")}
            />
          </div>
          {/* A caption, not a table: which phone, how recent; the mark on the picture
              already shows the last input. */}
          <div className="mt-3 w-full max-w-[280px] text-center text-[12.5px] text-muted">
            <p className="flex items-center justify-center gap-1.5">
              <span
                aria-hidden
                className={cn(
                  "h-1.5 w-1.5 shrink-0 rounded-full",
                  !why
                    ? "bg-green"
                    : device?.enabled
                      ? "bg-amber"
                      : "bg-slate/50",
                )}
              />
              <span className="truncate font-medium text-fg">
                {device?.name ?? t("common.device")}
              </span>
              {shotAt && !away && (
                <>
                  <span aria-hidden>·</span>
                  <Mono>{clock(shotAt, locale)}</Mono>
                </>
              )}
            </p>
            {/* Why the phone cannot be used now, as the sidebar and the composer say. */}
            {why && (
              <p id={whyId} className="mt-0.5 text-amber">
                {why}
              </p>
            )}
            {/* Not live: the picture is the last screenshot, which may be old. */}
            {shotAt && away && (
              <p className="mt-0.5" title={exactTime(shotAt, locale)}>
                {t("panel.shotFrom", { time: moment(shotAt, locale) })}
              </p>
            )}
            {obs?.foreground_app && (
              <p className="mt-0.5 truncate">
                <Mono title={obs.foreground_app}>{obs.foreground_app}</Mono>
              </p>
            )}
          </div>
          {/* A phone that cannot be used cannot be taken over either: it would send no
              picture. One that is only locked can: the takeover tries waking it. The
              reason shows on hover (a disabled button gets no hover). */}
          {canTakeOver && (
            <span
              className="mt-4 block w-full"
              title={away ? (why ?? away) : undefined}
            >
              <Button
                className="w-full"
                icon={Hand}
                busy={takingOver === active}
                disabled={!!away && !(device && takeoverable(device))}
                aria-describedby={away && why ? whyId : undefined}
                onClick={() => onTakeOver(active)}
              >
                {t("panel.takeOver")}
              </Button>
            </span>
          )}
        </section>
      )}

      {task.runs.length > 1 && (
        <section className="border-t border-line pt-4">
          <h3 className="mb-2 text-[13px] font-medium text-muted">
            {t("panel.agents")}
          </h3>
          {/* A sub-agent opens in a tab of its own: what it said and did. */}
          <ul className="-mx-2 space-y-0.5 text-[13px]">
            {task.runs.map((run) => {
              const name = agentName(run, task.runs, devices, t);
              const phone = devices.find((d) => d.id === run.device_ids[0]);
              const row = (
                <>
                  {phone ? (
                    <PlatformIcon platform={phone.platform} size={14} />
                  ) : (
                    <Ico icon={Bot} size={14} className="text-muted" />
                  )}
                  <span className="min-w-0 flex-1 truncate">{name}</span>
                  <span className="shrink-0 text-muted">
                    {runLabel(run, devices, t)}
                  </span>
                </>
              );
              return (
                <li key={run.id}>
                  {run.parent_run_id && onOpenAgent ? (
                    <button
                      type="button"
                      aria-label={t("agents.open", { name })}
                      title={run.goal}
                      onClick={() => onOpenAgent(run.id)}
                      className="flex h-8 w-full items-center gap-2 rounded-lg px-2 text-left hover:bg-hovered pointer-coarse:min-h-11"
                    >
                      {row}
                    </button>
                  ) : (
                    <div className="flex h-8 items-center gap-2 px-2">
                      {row}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <section className="border-t border-line pt-4 text-[12.5px] text-faint">
        {t("panel.usage", {
          calls: number(task.usage.model_calls, locale),
          input: number(task.usage.input_tokens, locale),
          output: number(task.usage.output_tokens, locale),
        })}
      </section>
    </div>
  );
}
