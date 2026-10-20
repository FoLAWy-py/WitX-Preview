import { useState } from "react";
import type { DeviceView, TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { DeviceFrame, thumbClass } from "./DeviceFrame";
import { ShotDialog } from "./ShotDialog";

const SHOTS = 3;

/** The screenshots the agent's answer rests on, under its words: the first few as
 * thumbnails, the rest behind a tile, each opening at full size. */
export function Evidence({
  task,
  ids,
  devices,
}: {
  task: TaskDetail;
  ids: string[];
  devices: DeviceView[];
}) {
  const { t } = useI18n();
  const [shot, setShot] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const shots = ids
    .map((id) => task.observations.find((o) => o.id === id))
    .filter((o): o is NonNullable<typeof o> => !!o && o.status === "complete");
  if (shots.length === 0) return null;
  const deviceOf = (id: string | null) =>
    devices.find(
      (d) => d.id === task.observations.find((o) => o.id === id)?.device_id,
    );
  return (
    <div className="mt-3 flex flex-wrap items-stretch gap-3">
      {(all || shots.length <= SHOTS ? shots : shots.slice(0, SHOTS)).map(
        (o) => (
          <div key={o.id} className={thumbClass(o)}>
            <DeviceFrame
              thumb
              platform={deviceOf(o.id)?.platform ?? "ios"}
              obs={o}
              label={t("timeline.openShot")}
              onClick={() => setShot(o.id)}
            />
          </div>
        ),
      )}
      {shots.length > SHOTS && (
        <button
          type="button"
          aria-expanded={all}
          onClick={() => setAll(!all)}
          className="press flex w-[92px] flex-col items-center justify-center gap-0.5 rounded-xl border border-dashed border-line-strong bg-sunken/60 text-muted hover:bg-sunken hover:text-fg"
        >
          {all ? (
            <span className="text-[13px] font-medium">
              {t("timeline.evidence.less")}
            </span>
          ) : (
            <>
              <span className="tnum text-[20px] font-semibold text-fg">
                +{shots.length - SHOTS}
              </span>
              <span className="text-[12px]">{t("timeline.evidence.more")}</span>
            </>
          )}
        </button>
      )}
      <ShotDialog
        task={task}
        shots={shots.map((o) => ({ id: o.id }))}
        open={shot}
        devices={devices}
        onClose={() => setShot(null)}
      />
    </div>
  );
}
