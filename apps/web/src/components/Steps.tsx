import { useState, type ReactNode } from "react";
import {
  AlertTriangle,
  AppWindow,
  ArrowLeft,
  BookOpen,
  ArrowUpDown,
  ChevronRight,
  Globe,
  House,
  ListChecks,
  ListOrdered,
  Lock,
  MousePointerClick,
  Pointer,
  ZoomIn,
  Repeat,
  RotateCcw,
  Search,
  Timer,
  Type,
  Volume1,
  Volume2,
  type LucideIcon,
} from "lucide-react";
import type { ActionView, DeviceView, TaskDetail } from "@/api/types.gen";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/format";
import { kindKey } from "@/lib/effects";
import type { WorkItem } from "@/lib/timeline";
import { EffectChips } from "./Approval";
import { ShotDialog } from "./ShotDialog";
import { Chip, Ico, Mono } from "./ui/base";
import { ResultWord, resultKind } from "./ui/status";
import { DeviceFrame, thumbClass } from "./DeviceFrame";

export const kindIcon: Record<ActionView["kind"], LucideIcon> = {
  tap: MousePointerClick,
  swipe: ArrowUpDown,
  long_press: Timer,
  double_tap: Pointer,
  pinch: ZoomIn,
  type_text: Type,
  set_picker: ListOrdered,
  select_option: ListChecks,
  back: ArrowLeft,
  home: House,
  volume_up: Volume2,
  volume_down: Volume1,
  lock: Lock,
  launch_app: AppWindow,
  find_app: Search,
};

/** Steps whose screenshot after says nothing: opening an app only brings it to the
 * front, and searching the installed apps leaves the screen as it was. The agent still
 * gets that screenshot; the timeline does not show it. */
const WITHOUT_SHOT: ReadonlySet<ActionView["kind"]> = new Set([
  "launch_app",
  "find_app",
]);

/** What an input did, in words: the target, or the text typed, or the app opened. */
export function stepText(action: ActionView): string {
  const input = action.input;
  if (action.kind === "type_text" && typeof input.text === "string")
    return `“${input.text}”`;
  if (action.kind === "launch_app" && typeof input.app_id === "string")
    return action.target || input.app_id;
  if (action.kind === "find_app" && typeof input.query === "string")
    return `“${input.query}”`;
  if (action.kind === "select_option" && typeof input.value === "string")
    return `“${input.value}”`;
  return action.target;
}

export function WhyPanel({ action }: { action: ActionView }) {
  const { t } = useI18n();
  const p = action.policy;
  if (!p) return null;
  return (
    <div className="grid gap-3 rounded-lg bg-sunken p-3 text-[13.5px] sm:grid-cols-2">
      <div>
        <div className="mb-1 flex flex-wrap items-center gap-2 font-medium">
          {t("timeline.policy")}
          <Chip tone={p.decision === "ask" ? "amber" : "neutral"}>
            {t(
              p.decision === "ask"
                ? "timeline.decision.ask"
                : "timeline.decision.allow",
            )}
          </Chip>
        </div>
        {p.reasons.length > 0 ? (
          <ul className="list-disc space-y-0.5 pl-4 text-muted">
            {p.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">{t("timeline.policy.none")}</p>
        )}
      </div>
      {p.risk && (
        <div>
          <div className="mb-1 flex flex-wrap items-center gap-2 font-medium">
            {t("timeline.risk")}
            <Chip
              tone={
                p.risk.failed ? "red" : p.risk.sensitive ? "amber" : "neutral"
              }
              icon={p.risk.sensitive ? AlertTriangle : undefined}
            >
              {t(
                p.risk.failed
                  ? "timeline.risk.failed"
                  : p.risk.sensitive
                    ? "timeline.risk.sensitive"
                    : "timeline.risk.notSensitive",
              )}
            </Chip>
          </div>
          <p className="text-muted">{p.risk.reason}</p>
        </div>
      )}
    </div>
  );
}

/** The screen after a step, when it says something (WITHOUT_SHOT) and was taken. */
function afterShot(task: TaskDetail, action: ActionView) {
  return action.post_observation_id && !WITHOUT_SHOT.has(action.kind)
    ? task.observations.find(
        (o) => o.id === action.post_observation_id && o.status === "complete",
      )
    : undefined;
}

function StepRow({
  task,
  action,
  device,
  showDevice,
  onShot,
}: {
  task: TaskDetail;
  action: ActionView;
  device: DeviceView | undefined;
  showDevice: boolean;
  /** Opens a screenshot in the round's gallery. */
  onShot: (id: string) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const kind = resultKind(action);
  const after = afterShot(task, action);
  const reason = action.reason && kind !== "sending" ? action.reason : null;
  // The screen after the step, why it was let through and the reason wait behind the
  // step; what it touches (money, messages) and how it ended, when not simply
  // done, never do.
  const details = !!(action.policy || reason || after);
  const flagged = action.effects.length > 0 || !!action.retry_of;
  const title = (
    <>
      <span>{t(kindKey(action))}</span>
      <span className="min-w-0 break-words">{stepText(action)}</span>
      {showDevice && device && (
        <span className="text-[12.5px] text-faint">· {device.name}</span>
      )}
      <ResultWord kind={kind} />
    </>
  );
  return (
    <li className="flex items-start gap-2.5 py-1.5">
      <span
        className={cn(
          "mt-[3px] flex h-5 w-5 shrink-0 items-center justify-center",
          kind === "unknown" ? "text-violet" : "text-faint",
        )}
      >
        <Ico icon={kindIcon[action.kind]} size={14} />
      </span>
      <div className="min-w-0">
        {details ? (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            className="group/step flex max-w-full cursor-pointer flex-wrap items-baseline gap-x-2 gap-y-0.5 py-0.5 text-left text-[14px] text-muted transition-colors hover:text-fg focus-visible:text-fg pointer-coarse:min-h-11"
          >
            {title}
            {/* Right after the words: what opens is what was just read. */}
            <ChevronRight
              size={13}
              strokeWidth={1.75}
              aria-hidden
              className={cn(
                "shrink-0 self-center transition-transform duration-150",
                open && "rotate-90",
              )}
            />
          </button>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-0.5 text-[14px] text-muted">
            {title}
          </div>
        )}
        {flagged && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <EffectChips effects={action.effects} action={action} />
            {action.retry_of && (
              <Chip icon={RotateCcw}>{t("timeline.retryOf")}</Chip>
            )}
          </div>
        )}
        {open && (
          <div className="anim-fade mt-2 space-y-2">
            {after && (
              <div className={thumbClass(after)}>
                <DeviceFrame
                  thumb
                  platform={device?.platform ?? "ios"}
                  obs={after}
                  label={t("timeline.after")}
                  onClick={() => onShot(after.id)}
                />
              </div>
            )}
            {reason && (
              <p className="text-[12.5px] text-faint">
                {t("timeline.reason")}:{" "}
                <Mono className="text-[12px]">{reason}</Mono>
              </p>
            )}
            <WhyPanel action={action} />
          </div>
        )}
      </div>
    </li>
  );
}

/** Icons of what the agent did along the way, besides its steps. */
const noticeIcon: Partial<Record<MessageKey, LucideIcon>> = {
  "timeline.sys.webSearchOn": Globe,
  "timeline.sys.webSearch": Search,
  "timeline.sys.webPage": Globe,
  "timeline.sys.skill": BookOpen,
  "timeline.sys.repeating": Repeat,
};

function NoticeRow({ item }: { item: Extract<WorkItem, { kind: "status" }> }) {
  const { t } = useI18n();
  return (
    <li className="flex items-start gap-2.5 py-1.5">
      <span className="mt-[3px] flex h-5 w-5 shrink-0 items-center justify-center text-faint">
        <Ico icon={noticeIcon[item.message] ?? Globe} size={14} />
      </span>
      <span className="min-w-0 py-0.5 text-[14px] break-words text-muted">
        {t(item.message, item.vars)}
      </span>
    </li>
  );
}

/** One round of the agent's work as one line. While it works, the line is what
 * it does right now (`live`: the shimmer, and how long this turn has gone); once done,
 * the kind of work it was, with no count or time. Its steps, and what it did along the
 * way (searched the web, loaded a skill), are lines under it only when unfolded. */
export function Round({
  task,
  items,
  devices,
  live,
}: {
  task: TaskDetail;
  items: WorkItem[];
  devices: DeviceView[];
  /** What the agent does right now, when this is the round under way. */
  live?: ReactNode;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [shot, setShot] = useState<string | null>(null);
  const actions = items.flatMap((i) => (i.kind === "action" ? [i.action] : []));
  // The round's screenshots, one gallery: opened from one step, it moves to the others.
  const shots = actions.flatMap((a) => {
    const after = afterShot(task, a);
    return after ? [{ id: after.id }] : [];
  });
  const several =
    new Set(actions.map((a) => a.device_id)).size > 1 || task.runs.length > 1;
  const device = (id: string) => devices.find((d) => d.id === id);
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex max-w-full items-center gap-3 py-1 text-left text-[13.5px] text-muted hover:text-fg"
      >
        {live ?? (
          <>
            <span
              aria-hidden
              className="flex h-6 w-6 shrink-0 items-center justify-center"
            >
              <span className="h-2 w-2 rounded-full bg-line-strong" />
            </span>
            <span className="truncate">
              {t(actions.length ? "timeline.worked" : "timeline.lookedUp")}
            </span>
          </>
        )}
        <ChevronRight
          size={14}
          strokeWidth={1.75}
          aria-hidden
          className={cn(
            "shrink-0 transition-transform duration-150",
            open && "rotate-90",
          )}
        />
      </button>
      {open && (
        // The lines hang from the round's line, under the conversation's gutter.
        <ol className="anim-fade ml-3 border-l border-line pl-[22px]">
          {items.map((i) =>
            i.kind === "action" ? (
              <StepRow
                key={i.key}
                task={task}
                action={i.action}
                device={device(i.action.device_id)}
                showDevice={several}
                onShot={setShot}
              />
            ) : (
              <NoticeRow key={i.key} item={i} />
            ),
          )}
        </ol>
      )}
      <ShotDialog
        task={task}
        shots={shots}
        open={shot}
        devices={devices}
        onClose={() => setShot(null)}
      />
    </div>
  );
}
