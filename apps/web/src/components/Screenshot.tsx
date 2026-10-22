import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/format";
import { useInReach } from "@/lib/reach";
import { shots, type Shot } from "@/lib/shots";

const NOTHING: Shot = { failed: false, purged: false };

/** A private screenshot (the session cookie signs the request), with `width` a small copy
 * that wide for a thumbnail. It loads once it scrolls near view and is let go again far
 * from it; every view of the same picture shares one copy (lib/shots.ts). */
export function Screenshot({
  observationId,
  alt,
  className,
  width,
}: {
  observationId: string;
  alt: string;
  className?: string;
  width?: number;
}) {
  const { api } = useAuth();
  const { t } = useI18n();
  const [box, setBox] = useState<Element | null>(null);
  const inReach = useInReach(box);
  const [shot, setShot] = useState(NOTHING);

  useEffect(() => {
    if (!inReach) {
      setShot(NOTHING);
      return;
    }
    return shots.watch(
      `${observationId}@${width ?? "full"}`,
      (signal) => api.screenshot(observationId, signal, width),
      setShot,
    );
  }, [api, observationId, width, inReach]);

  const { url, failed, purged } = shot;
  if (failed) {
    return (
      <div
        ref={setBox}
        className={cn(
          "flex flex-col items-center justify-center gap-1.5 bg-[#1c1d21] p-3 text-center text-[12px] text-[#8b8e96]",
          className,
        )}
      >
        <ImageOff
          size={18}
          strokeWidth={1.5}
          aria-label={t("shot.unavailable")}
        />
        {/* Kept as long as the task's maker chose: no number of days here. */}
        {purged && <span>{t("shot.purged")}</span>}
      </div>
    );
  }
  if (!url)
    return (
      <div ref={setBox} className={cn("skeleton rounded-none", className)} />
    );
  return (
    <img
      ref={setBox}
      src={url}
      alt={alt}
      draggable={false}
      className={cn("object-cover", className)}
    />
  );
}
