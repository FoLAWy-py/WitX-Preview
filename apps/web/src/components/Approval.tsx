import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import {
  Check,
  ChevronRight,
  ChevronsRight,
  Clock,
  CreditCard,
  ShieldAlert,
  X,
  FileCheck2,
  MessageSquare,
  Share2,
  Trash2,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import type { ActionView, DeviceView, TaskDetail } from "@/api/types.gen";
import { useI18n, type MessageKey } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { effectObject, kindKey } from "@/lib/effects";
import { cn, countdown, money } from "@/lib/format";
import { useLayout } from "@/lib/prefs";
import { DeviceFrame, THUMB_CLASS_WIDE, thumbClass } from "./DeviceFrame";
import { ShotDialog } from "./ShotDialog";
import { Button, Chip, Ico, inputClass } from "./ui/base";
import { Dialog } from "./ui/dialog";

/** Icons for what an input does. */
const effectIcon: Record<ActionView["effects"][number], LucideIcon> = {
  purchase: CreditCard,
  payment: CreditCard,
  send_message: MessageSquare,
  share_data: Share2,
  account_change: UserRound,
  delete_data: Trash2,
  change_data: FileCheck2,
};

/** One chip per effect. Given the step, each names what it changes ("Changes: plant
 * cherries", "Payment: ¥874") and shows the full expected effect on hover. */
export function EffectChips({
  effects,
  action,
}: {
  effects: ActionView["effects"];
  action?: ActionView;
}) {
  const { t, locale } = useI18n();
  return (
    <>
      {effects.map((e) => {
        const what = action ? effectObject(e, action, locale) : null;
        return (
          <Chip
            key={e}
            tone={e === "payment" || e === "purchase" ? "red" : "neutral"}
            icon={effectIcon[e]}
            title={action?.expected_effect || undefined}
            className={what ? "max-w-full" : undefined}
          >
            {what ? (
              <span className="truncate">
                {t("effect.named", {
                  effect: t(`effect.short.${e}` as MessageKey),
                  what,
                })}
              </span>
            ) : (
              t(`effect.${e}` as MessageKey)
            )}
          </Chip>
        );
      })}
    </>
  );
}

export const isPayment = (a: ActionView) =>
  a.effects.includes("payment") ||
  a.effects.includes("purchase") ||
  !!a.policy?.risk?.effects.some((e) => e === "payment" || e === "purchase");

export const isMessage = (a: ActionView) =>
  a.effects.includes("send_message") ||
  !!a.policy?.risk?.effects.includes("send_message");

export function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const h = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(h);
  }, [ms]);
  return now;
}

const TERM_ORDER = [
  "sender",
  "recipient",
  "body",
  "payee",
  "address",
  "payment_method",
  "amount",
  "currency",
];
/** Terms bound when the screen shows them. */
const SHOWN_PAYMENT_TERMS = ["address", "payment_method"];
const SHOWN_MESSAGE_TERMS = ["sender"];

/**
 * The exact terms an approval covers, in large type. A message's
 * sender, or a payment's delivery address or payment method, that the screen did not
 * show is said to be missing, so the person checks it on the phone.
 */
export function TermsList({
  terms,
  size = "lg",
  payment,
  message,
}: {
  terms: Record<string, string>;
  size?: "lg" | "md";
  payment?: boolean;
  message?: boolean;
}) {
  const { t, locale } = useI18n();
  const missing = [
    ...(payment ? SHOWN_PAYMENT_TERMS : []),
    ...(message ? SHOWN_MESSAGE_TERMS : []),
  ];
  const shown = {
    ...Object.fromEntries(missing.map((k) => [k, ""])),
    ...terms,
  };
  const entries = Object.entries(shown)
    .filter(([k]) => !(k === "currency" && terms.amount))
    .sort(([a], [b]) => TERM_ORDER.indexOf(a) - TERM_ORDER.indexOf(b));
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-3">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="pt-1 text-[13px] text-muted">
            {t(`terms.${k}` as MessageKey)}
          </dt>
          <dd
            className={cn(
              "min-w-0 break-words whitespace-pre-wrap",
              size === "lg"
                ? "text-[19px] leading-snug font-semibold"
                : "text-[15px] font-medium",
            )}
          >
            {!v ? (
              <span className="text-[13px] font-normal text-faint">
                {t("terms.notShown")}
              </span>
            ) : k === "amount" ? (
              <span
                className={cn(
                  "tnum text-red",
                  size === "lg"
                    ? "text-[30px] leading-none font-semibold tracking-tight"
                    : "text-[19px]",
                )}
              >
                {money(v, terms.currency ?? "", locale)}
              </span>
            ) : k === "body" ? (
              <span>“{v}”</span>
            ) : (
              <span className="tnum">{v}</span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Touch confirmation: drag the thumb to the end. Keyboard: arrow keys or End. */
export function SlideToConfirm({
  label,
  onConfirm,
  disabled,
  tone = "accent",
}: {
  label: string;
  onConfirm: () => void;
  disabled?: boolean;
  tone?: "accent" | "red";
}) {
  const track = useRef<HTMLDivElement>(null);
  const [x, setX] = useState(0);
  const [drag, setDrag] = useState(false);
  const start = useRef(0);
  const max = () => (track.current ? track.current.clientWidth - 56 : 1);
  const finish = (value: number) => {
    if (value >= max() * 0.92) {
      setX(max());
      onConfirm();
    } else setX(0);
  };
  const down = (e: PointerEvent) => {
    if (disabled) return;
    setDrag(true);
    start.current = e.clientX - x;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const move = (e: PointerEvent) =>
    drag && setX(Math.min(max(), Math.max(0, e.clientX - start.current)));
  const up = () => {
    if (!drag) return;
    setDrag(false);
    finish(x);
  };
  const key = (e: KeyboardEvent) => {
    if (disabled) return;
    if (e.key === "ArrowRight") {
      e.preventDefault();
      const next = Math.min(max(), x + max() / 4);
      setX(next);
      if (next >= max()) finish(next);
    }
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setX(Math.max(0, x - max() / 4));
    }
    if (e.key === "End") {
      e.preventDefault();
      finish(max());
    }
  };
  const pct = x / max();
  return (
    <div
      ref={track}
      className={cn(
        "relative h-14 w-full overflow-hidden rounded-full border select-none",
        disabled
          ? "border-line bg-sunken opacity-50"
          : tone === "red"
            ? "border-red/40 bg-red-soft"
            : "border-accent/30 bg-accent-soft",
      )}
    >
      <div
        className={cn(
          "absolute inset-y-0 left-0 rounded-full",
          tone === "red" ? "bg-red/15" : "bg-accent/15",
        )}
        style={{ width: x + 56 }}
      />
      <span
        className="absolute inset-0 flex items-center justify-center pl-12 text-[15px] font-medium text-fg"
        style={{ opacity: 1 - pct }}
      >
        {label}
      </span>
      <div
        role="slider"
        tabIndex={disabled ? -1 : 0}
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct * 100)}
        aria-disabled={disabled}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={up}
        onKeyDown={key}
        style={{
          transform: `translateX(${x}px)`,
          transition: drag ? "none" : "transform 180ms ease-out",
          touchAction: "none",
        }}
        className={cn(
          "absolute top-1 left-1 flex h-12 w-12 cursor-grab items-center justify-center rounded-full text-white",
          tone === "red" ? "bg-red" : "bg-accent",
        )}
      >
        <ChevronsRight size={22} strokeWidth={1.5} aria-hidden />
      </div>
    </div>
  );
}

/** How many steps a standing approval covers at most (rules.GRANT_MAX_USES). */
const GRANT_USES = 20;

/** The approval card: the one place a person lets a sensitive step happen. */
export function ApprovalCard({
  task,
  action,
  device,
  onChange,
  variant = "inline",
}: {
  task: TaskDetail;
  action: ActionView;
  device: DeviceView | undefined;
  onChange: () => Promise<void>;
  variant?: "inline" | "sheet";
}) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const { coarse } = useLayout();
  const now = useNow();
  const [typed, setTyped] = useState("");
  const [confirmPay, setConfirmPay] = useState(false);
  const [confirmSimilar, setConfirmSimilar] = useState(false);
  const [busy, setBusy] = useState<"approve" | "deny" | "switch" | "similar">();
  const [error, setError] = useState<string>();
  const [shot, setShot] = useState<string | null>(null);
  const pay = isPayment(action);
  const terms = action.intent ?? {};
  const amount = terms.amount ?? "";
  const formattedAmount = amount
    ? money(amount, terms.currency ?? "", locale)
    : "";
  const digits = (s: string) => s.replace(/[^\d.]/g, "");
  // What to type: the amount's number alone; its currency is in the terms above.
  const figure = digits(amount);
  const amountOk =
    !pay ||
    !amount ||
    (typed !== "" && Number(digits(typed)) === Number(digits(amount)));
  const left = action.approval
    ? new Date(action.approval.expires_at).getTime() - now
    : 0;
  const obs = task.observations.find((o) => o.id === action.observation_id);
  const slide = variant === "sheet" || coarse;

  async function decide(
    decision: "approve" | "deny",
    also?: "auto" | "similar",
  ) {
    setBusy(also === "auto" ? "switch" : (also ?? decision));
    setError(undefined);
    try {
      await api.decide(
        action.id,
        also === "similar" ? { decision, similar: true } : { decision },
      );
      if (also === "auto")
        await api.setPermissionMode(task.id, { permission_mode: "auto" });
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(undefined);
    }
  }

  const effects = [
    ...new Set([...action.effects, ...(action.policy?.risk?.effects ?? [])]),
  ];

  return (
    <section
      aria-label={t("approval.title")}
      className={cn(
        "rounded-xl bg-surface",
        variant === "inline" && "anim-glow border-[1.5px] border-amber",
      )}
    >
      <div
        className={cn(
          "flex flex-wrap items-center gap-3",
          variant === "inline"
            ? "border-b border-amber/25 px-5 py-4"
            : "px-4 pt-4",
        )}
      >
        <span className="flex h-8 w-8 items-center justify-center rounded-full bg-amber-soft text-amber">
          <Ico icon={pay ? CreditCard : ShieldAlert} size={18} />
        </span>
        {/* On a phone the title takes the row and the chips go below it. */}
        <div className="min-w-0 flex-1 basis-[calc(100%-3.5rem)] sm:basis-0">
          <h2 className="text-[16px] font-semibold">{t("approval.title")}</h2>
          <p className="truncate text-[13px] text-muted">
            {device?.name ?? t("common.device")} · {t(kindKey(action))} ·{" "}
            {action.target}
          </p>
        </div>
        <span className="flex flex-wrap gap-1.5">
          <EffectChips effects={effects} />
        </span>
        {action.approval && (
          <Chip tone="amber" icon={Clock}>
            <span className="tnum" aria-live="off">
              {t("approval.validFor", { time: countdown(left) })}
            </span>
          </Chip>
        )}
      </div>

      <div
        className={cn(
          "grid gap-5 p-4 sm:p-5",
          variant === "inline" &&
            (thumbClass(obs) === THUMB_CLASS_WIDE
              ? "md:grid-cols-[1fr_184px]"
              : "md:grid-cols-[1fr_92px]"),
        )}
      >
        <div className="min-w-0 space-y-4">
          {Object.keys(terms).length > 0 && (
            <div>
              <p className="mb-3 text-[13px] text-muted">
                {t("approval.covers")}
              </p>
              <TermsList
                terms={terms}
                payment={pay}
                message={isMessage(action)}
              />
            </div>
          )}
          {variant === "sheet" && obs && (
            <div className={cn("mx-auto", thumbClass(obs))}>
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
          {/* Why it waits and what should happen: the terms come first; with no
              terms to show, this is what there is to decide on, so it is open. */}
          <details
            open={Object.keys(terms).length === 0}
            className="group rounded-lg bg-sunken text-[14px]"
          >
            <summary className="flex cursor-pointer list-none items-center gap-1.5 px-4 py-2.5 text-[13px] font-medium text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
              <ChevronRight
                size={14}
                aria-hidden
                className="transition-transform group-open:rotate-90"
              />
              {t("approval.whyWaits")}
            </summary>
            <div className="grid gap-2 px-4 pb-3.5 text-muted">
              {action.policy && action.policy.reasons.length > 0 && (
                <ul className="list-disc space-y-1 pl-5">
                  {action.policy.reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              )}
              <p>
                <span className="text-faint">{t("timeline.expected")}: </span>
                {action.expected_effect}
              </p>
            </div>
          </details>
        </div>
        {variant === "inline" && obs && (
          <figure className={cn("hidden md:block", thumbClass(obs))}>
            <DeviceFrame
              thumb
              platform={device?.platform ?? "ios"}
              obs={obs}
              mark={action}
              label={t("approval.chosenOn")}
              onClick={() => setShot(obs.id)}
            />
            <figcaption className="mt-2 text-center text-[12px] text-muted">
              {t("approval.chosenOn")}
            </figcaption>
          </figure>
        )}
      </div>

      <div className="space-y-3 px-4 pb-4 sm:px-5 sm:pb-5">
        {pay && amount && (
          <div>
            <label
              htmlFor={`amount-${action.id}`}
              className="mb-1.5 block text-[13px] font-medium"
            >
              {t("approval.retype.hint", { amount: figure })}
            </label>
            <input
              id={`amount-${action.id}`}
              inputMode="decimal"
              autoComplete="off"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={figure}
              aria-invalid={!!typed && !amountOk}
              className={cn(
                inputClass,
                "font-mono text-[17px] tnum",
                typed && !amountOk && "border-red",
              )}
            />
          </div>
        )}
        {error && (
          <p role="alert" className="text-[13px] text-red">
            {error}
          </p>
        )}
        {slide ? (
          <div className="space-y-3">
            <SlideToConfirm
              label={t(pay ? "approval.slidePay" : "approval.slide")}
              tone={pay ? "red" : "accent"}
              disabled={!amountOk || !!busy}
              onConfirm={() =>
                pay ? setConfirmPay(true) : void decide("approve")
              }
            />
            <Button
              size="lg"
              className="w-full"
              icon={X}
              busy={busy === "deny"}
              onClick={() => void decide("deny")}
            >
              {t("approval.deny")}
            </Button>
            {!pay && (
              <Button
                variant="ghost"
                className="w-full"
                busy={busy === "similar"}
                onClick={() => setConfirmSimilar(true)}
              >
                {t("approval.approveSimilar")}
              </Button>
            )}
          </div>
        ) : (
          <div className="flex flex-wrap items-center justify-end gap-2">
            {!pay && (
              <Button
                variant="ghost"
                className="mr-auto"
                busy={busy === "similar"}
                onClick={() => setConfirmSimilar(true)}
              >
                {t("approval.approveSimilar")}
              </Button>
            )}
            {task.permission_mode === "ask" && !pay && (
              <Button
                variant="ghost"
                busy={busy === "switch"}
                onClick={() => void decide("approve", "auto")}
              >
                {t("approval.approveAndAuto")}
              </Button>
            )}
            <Button
              icon={X}
              busy={busy === "deny"}
              onClick={() => void decide("deny")}
            >
              {t("approval.deny")}
            </Button>
            <Button
              variant={pay ? "danger" : "primary"}
              icon={Check}
              disabled={!amountOk}
              busy={busy === "approve"}
              onClick={() =>
                pay ? setConfirmPay(true) : void decide("approve")
              }
            >
              {t("approval.approve")}
            </Button>
          </div>
        )}
      </div>
      <Dialog
        open={confirmSimilar}
        onClose={() => setConfirmSimilar(false)}
        tone="amber"
        title={t("approval.similar.title")}
        footer={
          <>
            <Button onClick={() => setConfirmSimilar(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              data-autofocus
              onClick={() => {
                setConfirmSimilar(false);
                void decide("approve", "similar");
              }}
            >
              {t("approval.similar.confirm")}
            </Button>
          </>
        }
      >
        <p className="text-fg">
          {t(
            terms.recipient
              ? "approval.similar.bodyTo"
              : "approval.similar.body",
            {
              uses: GRANT_USES,
              device: device?.name ?? t("common.device"),
              app: obs?.foreground_app ?? "",
              recipient: terms.recipient ?? "",
            },
          )}
        </p>
        <p className="mt-3 text-[13px] text-muted">
          {t("approval.similar.ends")}
        </p>
      </Dialog>
      <Dialog
        open={confirmPay}
        onClose={() => setConfirmPay(false)}
        tone="red"
        title={t("approval.payConfirm.title")}
        footer={
          <Button data-autofocus onClick={() => setConfirmPay(false)}>
            {t("common.cancel")}
          </Button>
        }
      >
        <p className="text-fg">
          {t("approval.payConfirm.body", {
            amount: formattedAmount || t("approval.payConfirm.unknownAmount"),
            payee: terms.payee ?? t("approval.payConfirm.unknownPayee"),
          })}
        </p>
        <div className="mt-4 rounded-lg bg-sunken p-3">
          <TermsList terms={terms} size="md" payment />
        </div>
        {/* A slide, not a click: paying is never one tap away. */}
        <div className="mt-4">
          <SlideToConfirm
            label={t("approval.slidePay")}
            tone="red"
            onConfirm={() => {
              setConfirmPay(false);
              void decide("approve");
            }}
          />
        </div>
      </Dialog>
      <ShotDialog
        task={task}
        shots={obs ? [{ id: obs.id, mark: action }] : []}
        open={shot}
        devices={[device]}
        onClose={() => setShot(null)}
      />
    </section>
  );
}
