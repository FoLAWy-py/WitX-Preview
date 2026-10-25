import {
  useLayoutEffect,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import type { LucideIcon } from "lucide-react";
import type { TakeoverInputResult } from "@/api/types.gen";
import type { MessageKey, Translate } from "@/i18n";
import { cn } from "@/lib/format";
import { KEY_ROOM, type Box, type Fitted } from "@/lib/screen";
import { Ico } from "../ui/base";

export type SideKind = "volume_up" | "volume_down" | "lock";

/** Where a phone's side keys are, as fractions of its height from the top: Android has
 * them all on the right, an iPhone its volume keys on the left. */
export const SIDE_KEYS: Record<
  "android" | "ios",
  { kind: SideKind; side: "left" | "right"; at: number; length: number }[]
> = {
  android: [
    { kind: "lock", side: "right", at: 0.19, length: 0.075 },
    { kind: "volume_up", side: "right", at: 0.31, length: 0.075 },
    { kind: "volume_down", side: "right", at: 0.388, length: 0.075 },
  ],
  ios: [
    { kind: "volume_up", side: "left", at: 0.2, length: 0.07 },
    { kind: "volume_down", side: "left", at: 0.285, length: 0.07 },
    { kind: "lock", side: "right", at: 0.24, length: 0.115 },
  ],
};
export const KEY_SHORTCUT: Record<SideKind, string> = {
  volume_up: "Alt ↑",
  volume_down: "Alt ↓",
  lock: "Alt L",
};

/** A key on the phone's edge, where it is on the phone. It is wider than it looks, so a
 * pointer finds it, and stands out a little from the frame like the real one. */
export function SideKey({
  spec,
  fit,
  coarse,
  label,
  state,
  disabled,
  onPress,
}: {
  spec: (typeof SIDE_KEYS)["android"][number];
  fit: Fitted;
  coarse: boolean;
  label: string;
  state: "idle" | "sent" | "failed";
  disabled: boolean;
  onPress: () => void;
}) {
  const outerWidth = fit.width + 2 * fit.bezel;
  const outerHeight = fit.height + 2 * fit.bezel;
  const length = Math.round(outerHeight * spec.length);
  const hitLength = Math.max(length, coarse ? 44 : 24);
  const hitWidth = coarse ? KEY_ROOM + 12 : KEY_ROOM + 2;
  const right = spec.side === "right";
  const bar = fit.bezel >= 9 ? 5 : 4;
  return (
    <button
      type="button"
      aria-label={label}
      title={`${label} (${KEY_SHORTCUT[spec.kind]})`}
      disabled={disabled}
      onClick={onPress}
      className="group absolute z-[1] disabled:cursor-not-allowed"
      style={{
        top: Math.round(outerHeight * spec.at + length / 2 - hitLength / 2),
        left: right ? outerWidth - 3 : -(hitWidth - 3),
        width: hitWidth,
        height: hitLength,
      }}
    >
      <span
        aria-hidden
        className={cn(
          "absolute transition-[background-color,translate] duration-100",
          state === "sent"
            ? "bg-accent"
            : state === "failed"
              ? "bg-amber"
              : "bg-[#2c2e34] group-hover:bg-[#4a4d57] group-active:bg-accent group-disabled:bg-[#2c2e34] dark:bg-[#4a4d57] dark:group-hover:bg-[#6b6f78] dark:group-disabled:bg-[#4a4d57]",
          right ? "rounded-r-[3px]" : "rounded-l-[3px]",
          state === "sent" && (right ? "-translate-x-px" : "translate-x-px"),
        )}
        style={{
          top: (hitLength - length) / 2,
          height: length,
          width: bar,
          [right ? "left" : "right"]: 0,
          boxShadow:
            "inset 0 1px 0 rgb(255 255 255 / 0.14), inset 0 -1px 0 rgb(0 0 0 / 0.4)",
        }}
      />
    </button>
  );
}

/** How a side key looks after its last press: pressed while it runs, amber for a moment
 * when it did not. */
export function keyState(
  recent: Recent[],
  kind: string,
  now: number,
): "idle" | "sent" | "failed" {
  const last = recent.find((r) => r.kind === kind);
  if (!last) return "idle";
  if (last.state === "sent") return "sent";
  if (last.state !== "done" && now - last.at < 3_000) return "failed";
  return "idle";
}

export function StatusChip({
  tone,
  label,
}: {
  tone: "live" | "connecting" | "delayed";
  label: string;
}) {
  return (
    <span className="inline-flex h-7 shrink-0 items-center gap-2 rounded-full bg-sunken px-2.5 text-[12.5px] font-medium whitespace-nowrap text-muted">
      <span
        aria-hidden
        className={cn(
          "h-[7px] w-[7px] rounded-full",
          tone === "live"
            ? "anim-pulse bg-accent"
            : tone === "delayed"
              ? "bg-amber"
              : "bg-line-strong",
        )}
      />
      {label}
    </span>
  );
}

/** A message over the picture: what needs the person now. White, with only its icon in
 * the colour that says so, to keep the picture the thing to look at. */
export function Toast({
  icon,
  action,
  children,
}: {
  icon: LucideIcon;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      role="status"
      className="elev anim-slide pointer-events-auto flex w-full max-w-[420px] items-center gap-2.5 rounded-xl border border-line bg-surface py-2 pr-2 pl-3 text-[13px]"
    >
      <Ico icon={icon} size={16} className="text-amber" />
      <span className="min-w-0 flex-1">{children}</span>
      {action}
    </div>
  );
}

/** The size of an element, as it changes. */
export function useBoxSize(ref: RefObject<HTMLElement | null>): Box {
  const [size, setSize] = useState<Box>({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () =>
      setSize((old) =>
        old.width === el.clientWidth && old.height === el.clientHeight
          ? old
          : { width: el.clientWidth, height: el.clientHeight },
      );
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

/** A press shown on the picture: sent, then done or failed. */
export type Mark = {
  id: string;
  input: string;
  x: number;
  y: number;
  state: "sent" | "done" | "failed";
};

/** An input in the list of recent ones: its kind (for typing, how many characters, never
 * which), and what came of it. */
export type Recent = {
  id: string;
  kind: string;
  chars?: number;
  at: number;
  state: "sent" | "done" | "failed" | "unknown";
  why?: string;
};

/** Why an input did not run, in the person's words. */
export function why(result: TakeoverInputResult, t: Translate): string {
  const reason = result.reason ?? "";
  const known: Record<string, MessageKey> = {
    stale_frame: "takeover.why.stale",
    out_of_order: "takeover.why.stale",
    screen_changed: "takeover.why.screenChanged",
    stale_epoch: "takeover.why.over",
    stopped: "takeover.why.over",
    out_of_bounds: "takeover.why.outside",
    unsupported_input: "takeover.why.unsupported",
    locked: "takeover.why.locked",
    timeout: "takeover.why.slow",
  };
  const words = known[reason]
    ? t(known[reason])
    : t("takeover.why.other", { reason: reason.replace(/_/g, " ") || "?" });
  return t(
    result.outcome === "unknown" ? "takeover.notSure" : "takeover.notDone",
    { why: words },
  );
}

export function Frame({
  label,
  inline,
  row,
  rootRef,
  onKeyDown,
  children,
}: {
  label: string;
  inline?: boolean;
  row?: boolean;
  rootRef: RefObject<HTMLDivElement | null>;
  onKeyDown: (event: KeyboardEvent) => void;
  children: ReactNode;
}) {
  // Focusable, so the side keys' shortcuts work after a click on the picture. Full
  // screen it lies over the navigation drawer (z 70) and under the sheets, dialogs and
  // menus (z 80, 90) it opens itself, such as its menu of endings.
  return inline ? (
    <section
      ref={rootRef}
      aria-label={label}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="flex min-h-full flex-col bg-surface outline-none"
    >
      {children}
    </section>
  ) : (
    <div
      ref={rootRef}
      role="dialog"
      aria-modal
      aria-label={label}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className={cn(
        "anim-fade fixed inset-0 z-[75] flex bg-bg pt-[env(safe-area-inset-top)] outline-none",
        row ? "flex-row" : "flex-col",
      )}
    >
      {children}
    </div>
  );
}
