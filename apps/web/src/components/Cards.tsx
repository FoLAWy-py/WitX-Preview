import { useState } from "react";
import {
  Check,
  ChevronRight,
  Clock,
  Hand,
  HelpCircle,
  Lock,
  X,
} from "lucide-react";
import type { ActionView, DeviceView, TaskDetail } from "@/api/types.gen";
import { useI18n, type MessageKey } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { clock, cn, money } from "@/lib/format";
import { kindKey } from "@/lib/effects";
import { unavailable, usable } from "@/lib/pool";
import { screenshotAfter } from "@/lib/timeline";
import { DeviceFrame, thumbClass } from "./DeviceFrame";
import { Button, Ico } from "./ui/base";
import { ShotDialog } from "./ShotDialog";
import { stepText, WhyPanel } from "./Steps";
import { isMessage, isPayment, TermsList } from "./Approval";

/** An input whose effect is unknown: the person settles it from a newer screenshot. */
export function UnknownCard({
  task,
  action,
  device,
  onChange,
  onTakeOver,
}: {
  task: TaskDetail;
  action: ActionView;
  device: DeviceView | undefined;
  onChange: () => Promise<void>;
  onTakeOver?: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [shot, setShot] = useState<string | null>(null);
  const before = task.observations.find((o) => o.id === action.observation_id);
  const evidence = screenshotAfter(task, action);
  const after = task.observations.find((o) => o.id === evidence);
  const away = device && !usable(device) ? unavailable(device, t) : undefined;

  async function settle(effect: "success" | "no_effect") {
    if (!evidence) return;
    setBusy(effect);
    setError(undefined);
    try {
      await api.reconcile(action.id, { effect, observation_id: evidence });
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <section className="rounded-xl border-[1.5px] border-violet bg-surface">
      <div className="flex items-start gap-3 border-b border-violet/25 px-5 py-4">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-violet-soft text-violet">
          <Ico icon={HelpCircle} size={18} />
        </span>
        <div>
          <h2 className="text-[16px] font-semibold">{t("unknown.title")}</h2>
          <p className="mt-0.5 text-[13.5px] text-muted">{t("unknown.body")}</p>
        </div>
      </div>
      <div className="space-y-4 p-5">
        <p className="flex items-start gap-2 rounded-lg bg-violet-soft px-3 py-2.5 text-[14px] text-violet">
          <Ico icon={Lock} size={16} className="mt-0.5" />
          {t("unknown.locked", { device: device?.name ?? t("common.device") })}
        </p>
        <p className="text-[14px]">
          <span className="text-muted">{t(kindKey(action))} · </span>
          {stepText(action)}
        </p>
        <div className="flex flex-wrap gap-6">
          <figure>
            <figcaption className="mb-2 text-[13px] font-medium text-muted">
              {t("unknown.before")}
            </figcaption>
            <div className={thumbClass(before)}>
              <DeviceFrame
                thumb
                platform={device?.platform ?? "ios"}
                obs={before}
                mark={action}
                label={t("unknown.before")}
                onClick={before ? () => setShot(before.id) : undefined}
              />
            </div>
          </figure>
          <figure>
            <figcaption className="mb-2 text-[13px] font-medium text-muted">
              {t("unknown.after")}
            </figcaption>
            <div className={thumbClass(after)}>
              <DeviceFrame
                thumb
                platform={device?.platform ?? "ios"}
                obs={after}
                label={t("unknown.after")}
                empty={t("unknown.needAfter")}
                onClick={after ? () => setShot(after.id) : undefined}
              />
            </div>
          </figure>
        </div>
        {evidence && (
          <p className="text-[13px] text-muted">{t("unknown.compare")}</p>
        )}
        {error && (
          <p role="alert" className="text-[13px] text-red">
            {error}
          </p>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
          {/* A phone that cannot be used now cannot be taken over: the reason on hover. */}
          {!evidence && onTakeOver && (
            <span className="mr-auto" title={away}>
              <Button icon={Hand} disabled={!!away} onClick={onTakeOver}>
                {t("unknown.lookYourself")}
              </Button>
            </span>
          )}
          <Button
            icon={X}
            disabled={!evidence}
            busy={busy === "no_effect"}
            onClick={() => void settle("no_effect")}
          >
            {t("unknown.didNot")}
          </Button>
          <Button
            variant="primary"
            icon={Check}
            disabled={!evidence}
            busy={busy === "success"}
            onClick={() => void settle("success")}
          >
            {t("unknown.happened")}
          </Button>
        </div>
      </div>
      <ShotDialog
        task={task}
        // Before the input, with it marked, and after it.
        shots={[
          ...(before ? [{ id: before.id, mark: action }] : []),
          ...(after ? [{ id: after.id }] : []),
        ]}
        open={shot}
        devices={[device]}
        onClose={() => setShot(null)}
      />
    </section>
  );
}

/** Evidence shown before the rest fold away. */
/** A decision on an approval, kept as a line of the conversation: what it covered, the
 * amount if any, and when. Fresh from the person's hand, it lights up once. A step a
 * standing approval let through says so; the approval that started one shows how many
 * steps it still covers, with a way to revoke it. The line opens to show what was
 * decided on: the input, the screen it was chosen on, the terms, and why it waited. */
export function ApprovalRecord({
  task,
  action,
  device,
  onChange,
}: {
  task: TaskDetail;
  action: ActionView;
  device: DeviceView | undefined;
  onChange: () => Promise<void>;
}) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const [revoking, setRevoking] = useState(false);
  const [open, setOpen] = useState(false);
  const [shot, setShot] = useState<string | null>(null);
  const approval = action.approval!;
  const state =
    approval.status === "denied"
      ? "denied"
      : approval.status === "expired"
        ? "expired"
        : approval.grant_id
          ? "byGrant"
          : "approved";
  const grant = task.grants.find((g) => g.created_from === action.id);
  const at = approval.decided_at ?? action.created_at;
  const fresh = Date.now() - Date.parse(at) < 8000;
  const amount = action.intent?.amount
    ? money(action.intent.amount, action.intent.currency ?? "", locale)
    : undefined;
  const obs = task.observations.find((o) => o.id === action.observation_id);
  const terms = action.intent ?? {};
  return (
    <div className={cn("rounded-lg", open && "bg-sunken/60")}>
      <div
        className={cn(
          "flex items-center gap-2 rounded-lg px-3 py-2 text-[13.5px]",
          fresh && "anim-flash",
        )}
      >
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="group/rec flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left text-muted transition-colors hover:text-fg focus-visible:text-fg"
        >
          <Ico
            icon={state === "approved" ? Check : state === "denied" ? X : Clock}
            size={15}
            className={cn(
              "shrink-0",
              state === "approved"
                ? "text-green"
                : state === "denied"
                  ? "text-red"
                  : "text-faint",
            )}
          />
          <span className="min-w-0 truncate">
            {t(`approval.record.${state}` as MessageKey, {
              what: action.target,
            })}
            {amount && <span className="tnum font-medium"> · {amount}</span>}
            {grant && (
              <span>
                {" · "}
                {grant.status === "active"
                  ? t("grant.active", { left: grant.max_uses - grant.uses })
                  : t("grant.ended")}
              </span>
            )}
          </span>
          <ChevronRight
            size={14}
            strokeWidth={1.75}
            aria-hidden
            className={cn(
              "shrink-0 transition-transform duration-150",
              open && "rotate-90",
            )}
          />
          <span className="flex-1" />
        </button>
        {grant?.status === "active" && (
          <Button
            size="sm"
            variant="ghost"
            busy={revoking}
            onClick={async () => {
              setRevoking(true);
              try {
                await api.revokeGrant(grant.id);
                await onChange();
              } finally {
                setRevoking(false);
              }
            }}
          >
            {t("grant.revoke")}
          </Button>
        )}
        <span className="tnum shrink-0 text-[12px] text-faint">
          {clock(at, locale)}
        </span>
      </div>
      {open && (
        <div className="anim-fade flex gap-4 px-3 pb-3">
          {obs && (
            <div className={thumbClass(obs)}>
              <DeviceFrame
                thumb
                platform={device?.platform ?? "ios"}
                obs={obs}
                mark={action}
                label={t("approval.chosenOn")}
                onClick={() => setShot(obs.id)}
              />
            </div>
          )}
          <div className="min-w-0 flex-1 space-y-3 text-[13.5px]">
            <p>
              <span className="text-muted">{t(kindKey(action))} · </span>
              {stepText(action)}
            </p>
            {Object.keys(terms).length > 0 && (
              <TermsList
                terms={terms}
                payment={isPayment(action)}
                message={isMessage(action)}
              />
            )}
            <WhyPanel action={action} />
          </div>
        </div>
      )}
      <ShotDialog
        task={task}
        shots={obs ? [{ id: obs.id, mark: action }] : []}
        open={shot}
        devices={[device]}
        onClose={() => setShot(null)}
      />
    </div>
  );
}
