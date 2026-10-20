import {
  Check,
  CircleDot,
  CirclePause,
  CircleStop,
  Loader,
  OctagonX,
  Smartphone,
  type LucideIcon,
} from "lucide-react";
import type { ActionView, TaskDetail } from "@/api/types.gen";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/format";
import { Tone, Chip } from "./base";

const taskStatus: Record<
  TaskDetail["status"],
  { tone: Tone; icon: LucideIcon; pulse?: boolean }
> = {
  active: { tone: "accent", icon: CircleDot, pulse: true },
  pausing: { tone: "slate", icon: Loader },
  paused: { tone: "slate", icon: CirclePause },
  stopping: { tone: "slate", icon: Loader },
  stopped: { tone: "slate", icon: CircleStop },
  completed: { tone: "green", icon: Check },
  failed: { tone: "red", icon: OctagonX },
};

/** The task's state. A running task whose main agent waits for its phones to come back
 * (`waitsForPhone`) says so instead of "running": nothing moves until they do. */
export function TaskStatusChip({
  status,
  waitingForPhone,
}: {
  status: TaskDetail["status"];
  waitingForPhone?: boolean;
}) {
  const { t } = useI18n();
  if (status === "active" && waitingForPhone)
    return (
      <Chip tone="amber" icon={Smartphone}>
        {t("status.waitingPhone")}
      </Chip>
    );
  const s = taskStatus[status];
  return (
    <Chip tone={s.tone} icon={s.icon} pulse={s.pulse}>
      {t(`status.${status}` as MessageKey)}
    </Chip>
  );
}

/** A small dot for lists: the task's state at a glance. */
export function StatusDot({
  status,
  needsYou,
}: {
  status: TaskDetail["status"];
  needsYou?: boolean;
}) {
  const tone = needsYou
    ? "bg-amber"
    : status === "active"
      ? "bg-accent anim-pulse"
      : status === "completed"
        ? "bg-green"
        : status === "failed"
          ? "bg-red"
          : "bg-slate/60";
  return (
    <span aria-hidden className={cn("h-2 w-2 shrink-0 rounded-full", tone)} />
  );
}

export type ResultKind =
  | ActionView["effect"]
  | "awaiting"
  | "sending"
  | "denied"
  | "cancelled"
  | "rejected";

/** What happened to an input: whether it was sent, and what is known of its effect. */
export function resultKind(action: ActionView): ResultKind {
  switch (action.status) {
    case "awaiting_approval":
      return "awaiting";
    case "queued":
    case "sent":
      return "sending";
    case "denied":
    case "cancelled":
    case "rejected":
      return action.status;
    case "done":
      return action.effect;
  }
}

/** How a step ended, in a word after it, only when it did not simply go through: an
 * input under way or waiting for you says nothing here (the line under the
 * conversation and the approval say it), and an unknown effect, which you must settle,
 * stands out. */
export function ResultWord({ kind }: { kind: ResultKind }) {
  const { t } = useI18n();
  if (kind === "unverified" || kind === "sending" || kind === "awaiting")
    return null;
  return (
    <span
      className={cn(
        "text-[12.5px]",
        kind === "unknown"
          ? "font-medium text-violet"
          : kind === "success"
            ? "text-green"
            : "text-faint",
      )}
    >
      · {t(`result.${kind}` as MessageKey)}
    </span>
  );
}
