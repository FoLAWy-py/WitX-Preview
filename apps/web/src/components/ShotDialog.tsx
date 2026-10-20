import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { DeviceView, TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { clock, cn } from "@/lib/format";
import { DeviceFrame, type InputMark } from "./DeviceFrame";
import { Screenshot } from "./Screenshot";
import { Button, Mono, PlatformIcon } from "./ui/base";
import { Dialog } from "./ui/dialog";

/** A screenshot it shows, with the input to mark on it, if any. */
export interface Shot {
  id: string;
  mark?: InputMark | null;
}

/** What the window leaves around a portrait picture: the dialog's title, caption and
 * row of thumbnails, and its margins. */
const AROUND = 230;

/** Screenshots at full size, opened from a thumbnail in the conversation, as a gallery
 * of the pictures shown where it was opened: an answer's evidence, a round's
 * steps. The phone fits the window's height; arrows, ← and →, and a row of thumbnails
 * move between them. */
export function ShotDialog({
  task,
  shots,
  open,
  devices,
  onClose,
}: {
  task: TaskDetail;
  shots: Shot[];
  /** The one it opens on; null while closed. */
  open: string | null;
  devices: (DeviceView | undefined)[];
  onClose: () => void;
}) {
  const { t, locale } = useI18n();
  // Moved to by the person; until then, the one it was opened on.
  const [picked, setPicked] = useState<string | null>(null);
  const current = shots.findIndex((s) => s.id === (picked ?? open));
  const at = current < 0 ? 0 : current;
  const shot = open ? shots[at] : undefined;
  const obs = task.observations.find((o) => o.id === shot?.id);
  const device = devices.find((d) => d?.id === obs?.device_id) ?? devices[0];
  const several = shots.length > 1;
  const go = (step: number) =>
    setPicked(shots[(at + step + shots.length) % shots.length].id);
  const close = () => {
    setPicked(null);
    onClose();
  };

  useEffect(() => {
    if (!open || !several) return;
    const key = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      go(e.key === "ArrowLeft" ? -1 : 1);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const wide = (obs?.width ?? 0) > (obs?.height ?? 1);
  const ratio = (obs?.width ?? 9) / (obs?.height ?? 19.5);
  const arrow = (step: number) =>
    several && (
      <Button
        variant="ghost"
        iconOnly
        icon={step < 0 ? ChevronLeft : ChevronRight}
        aria-label={t(step < 0 ? "shot.previous" : "shot.next")}
        onClick={() => go(step)}
        className="shrink-0"
      />
    );
  return (
    <Dialog
      open={!!open}
      onClose={close}
      wide
      title={
        <span className="flex items-center gap-2">
          {device && <PlatformIcon platform={device.platform} />}
          {device?.name ?? t("common.device")}
        </span>
      }
    >
      {obs && (
        <div className="space-y-3">
          <div className="flex items-center justify-center gap-2">
            {arrow(-1)}
            <div
              className="min-w-0"
              // As tall as the window allows, at the screen's shape.
              style={{
                width: wide
                  ? "100%"
                  : `min(100%, calc((92dvh - ${several ? AROUND : AROUND - 80}px) * ${ratio}))`,
              }}
            >
              <DeviceFrame
                platform={device?.platform ?? "ios"}
                obs={obs}
                mark={shot?.mark}
                label={t("timeline.screenshot")}
              />
            </div>
            {arrow(1)}
          </div>
          <div className="flex items-center justify-between gap-2 text-[12px]">
            <Mono>{obs.foreground_app ?? ""}</Mono>
            {several && (
              <span className="tnum text-muted">
                {t("shot.of", {
                  n: String(at + 1),
                  count: String(shots.length),
                })}
              </span>
            )}
            <Mono>{obs.captured_at ? clock(obs.captured_at, locale) : ""}</Mono>
          </div>
          {several && (
            <div className="flex gap-2 overflow-x-auto pb-1">
              {shots.map((s, i) => (
                <button
                  key={s.id}
                  type="button"
                  aria-label={t("shot.pick", { n: String(i + 1) })}
                  aria-current={i === at || undefined}
                  // The one shown stays in sight as the arrows move along the row.
                  ref={
                    i === at
                      ? (el) =>
                          el?.scrollIntoView({
                            block: "nearest",
                            inline: "nearest",
                          })
                      : undefined
                  }
                  onClick={() => setPicked(s.id)}
                  className={cn(
                    "press shrink-0 overflow-hidden rounded-md border",
                    i === at
                      ? "border-accent ring-2 ring-accent/40"
                      : "border-line opacity-70 hover:opacity-100",
                  )}
                >
                  <Screenshot
                    observationId={s.id}
                    alt=""
                    width={96}
                    className="block h-14 w-auto"
                  />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
