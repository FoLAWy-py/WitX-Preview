import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import { ApiError } from "@/api/client";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/format";

/** Loads a private screenshot (the session cookie signs the request) and shows it;
 * with `width`, a small copy of it that wide, for a thumbnail. */
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
  const [url, setUrl] = useState<string>();
  const [failed, setFailed] = useState(false);
  const [purged, setPurged] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;
    setUrl(undefined);
    setFailed(false);
    setPurged(false);
    api
      .screenshot(observationId, controller.signal, width)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((error: Error) => {
        if (error.name === "AbortError") return;
        if (error instanceof ApiError && error.code === "purged")
          setPurged(true);
        setFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, observationId, width]);

  if (failed) {
    return (
      <div
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
  if (!url) return <div className={cn("skeleton rounded-none", className)} />;
  return (
    <img
      src={url}
      alt={alt}
      draggable={false}
      className={cn("object-cover", className)}
    />
  );
}
