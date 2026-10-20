import { useState } from "react";
import { Check, ShoppingCart } from "lucide-react";
import type { DeviceView, PurchaseView, TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn, money } from "@/lib/format";
import { DeviceFrame } from "./DeviceFrame";
import { Button, Ico } from "./ui/base";

/**
 * Where the agent proposes to buy, in a task with several phones: one
 * option per phone, with the terms its checkout showed. The person chooses one, or
 * none; only the chosen phone may then order or pay, on those terms.
 */
export function PurchaseCard({
  task,
  purchase,
  devices,
  onChange,
}: {
  task: TaskDetail;
  purchase: PurchaseView;
  devices: DeviceView[];
  onChange: () => Promise<void>;
}) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState<number | "none">();
  const [error, setError] = useState<string>();
  const open = purchase.status === "proposed";

  async function choose(option: number | null) {
    setBusy(option ?? "none");
    setError(undefined);
    try {
      await api.choosePurchase(purchase.id, { option });
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <section
      aria-label={t("purchase.title")}
      className={cn(
        "rounded-xl border bg-surface px-4 py-3",
        open ? "border-amber" : "border-line",
      )}
    >
      <div className="mb-1 flex items-center gap-1.5 text-[12.5px] font-medium text-muted">
        <Ico icon={ShoppingCart} size={14} />
        {t("purchase.title")}
      </div>
      <p className="text-[15px] break-words">{purchase.goal}</p>
      <ul className="mt-3 grid gap-2">
        {purchase.options.map((option, i) => {
          const device = devices.find((d) => d.id === option.device_id);
          const obs = task.observations.find(
            (o) => o.id === option.observation_id,
          );
          const chosen = purchase.chosen === i;
          return (
            <li
              key={i}
              className={cn(
                "flex items-center gap-3 rounded-lg bg-sunken p-3",
                chosen && "ring-1 ring-green",
              )}
            >
              {obs && (
                <div className="w-[44px] shrink-0">
                  <DeviceFrame
                    thumb
                    platform={device?.platform ?? "ios"}
                    obs={obs}
                    label={t("purchase.screenshot")}
                  />
                </div>
              )}
              <div className="min-w-0 flex-1">
                <p className="text-[15px] font-semibold break-words">
                  {option.payee}
                  <span className="tnum ml-2 text-red">
                    {money(option.amount, option.currency, locale)}
                  </span>
                </p>
                <p className="text-[13px] break-words text-muted">
                  {device?.name ?? t("common.device")} · {option.summary}
                </p>
              </div>
              {open ? (
                <Button
                  size="sm"
                  variant="primary"
                  busy={busy === i}
                  disabled={busy !== undefined}
                  onClick={() => void choose(i)}
                >
                  {t("purchase.choose")}
                </Button>
              ) : (
                chosen && (
                  <span className="flex shrink-0 items-center gap-1 text-[13px] text-green">
                    <Ico icon={Check} size={14} />
                    {t("purchase.chosen")}
                  </span>
                )
              )}
            </li>
          );
        })}
      </ul>
      {open && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <p className="text-[13px] text-muted">{t("purchase.onlyChosen")}</p>
          <Button
            size="sm"
            busy={busy === "none"}
            disabled={busy !== undefined}
            onClick={() => void choose(null)}
          >
            {t("purchase.none")}
          </Button>
        </div>
      )}
      {purchase.status === "declined" && (
        <p className="mt-2 text-[13px] text-muted">{t("purchase.declined")}</p>
      )}
      {purchase.status === "superseded" && (
        <p className="mt-2 text-[13px] text-faint">
          {t("purchase.superseded")}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </section>
  );
}
