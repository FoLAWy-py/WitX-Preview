import { useEffect, useState, type FormEvent } from "react";
import {
  Check,
  ChevronRight,
  Circle,
  CircleDot,
  Pause,
  Pencil,
  Play,
  Target,
  Trash2,
} from "lucide-react";
import type { TaskDetail } from "@/api/types.gen";
import { useI18n, type MessageKey, type Translate } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { clock, cn, duration, number } from "@/lib/format";
import type { TimelineItem } from "@/lib/timeline";
import { Button, Chip, Ico, inputClass } from "./ui/base";
import { Dialog } from "./ui/dialog";

/**
 * The time the agent has worked on the goal: only while it calls the model, works a
 * phone or takes a screenshot, not while it waits for you; still while the goal is
 * paused. The server counts; between its updates, this keeps counting while it works.
 */
function useGoalTime(task: TaskDetail): number {
  const running = task.working && task.status === "active" && !task.goal_paused;
  const [base, setBase] = useState({ ms: task.goal_ms, at: Date.now() });
  if (base.ms !== task.goal_ms) setBase({ ms: task.goal_ms, at: Date.now() });
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  return base.ms + (running ? Math.max(0, now - base.at) : 0);
}

/**
 * The goal in one line above the message box, like Claude Code's: what the task is for,
 * how long it has run and how many model calls it took. Opens the goal in the panel.
 */
export function GoalBar({
  task,
  onOpen,
}: {
  task: TaskDetail;
  onOpen: () => void;
}) {
  const { t, locale } = useI18n();
  const worked = useGoalTime(task);
  return (
    <button
      type="button"
      onClick={onOpen}
      title={task.goal || undefined}
      className="press group mb-1.5 flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left text-[12.5px] text-muted hover:bg-sunken hover:text-fg pointer-coarse:min-h-10"
    >
      <Ico icon={Target} size={14} className="shrink-0 text-accent" />
      <span className="shrink-0 font-medium text-fg">{t("goal.title")}</span>
      <span
        className={cn(
          "min-w-0 flex-1 truncate",
          task.goal_paused && "line-through decoration-faint",
        )}
      >
        {task.goal || t("goal.noneShort")}
      </span>
      {task.goal_paused && <Chip tone="slate">{t("goal.paused")}</Chip>}
      <span className="tnum shrink-0 text-faint max-sm:hidden">
        {t("goal.meta", {
          time: duration(worked, t),
          calls: number(task.usage.model_calls, locale),
        })}
      </span>
      <ChevronRight
        size={14}
        aria-hidden
        className="shrink-0 text-faint transition-transform group-hover:translate-x-0.5"
      />
    </button>
  );
}

/** The moments of a task worth finding again: what you said, what the agent asked,
 * decisions on approvals, goal and status changes, the result. Ordinary steps stay
 * in the conversation. */
function milestones(items: TimelineItem[], t: Translate) {
  const out: { key: string; at: string; text: string }[] = [];
  for (const item of items) {
    if (item.kind === "input")
      out.push({ key: item.key, at: item.at, text: item.input.text });
    else if (item.kind === "question")
      out.push({
        key: item.key,
        at: item.at,
        text: t("progress.asked", { text: item.text }),
      });
    else if (item.kind === "status")
      out.push({
        key: item.key,
        at: item.at,
        text: t(item.message, item.vars),
      });
    else if (item.kind === "action" && item.action.approval) {
      const status = item.action.approval.status;
      const state =
        status === "pending"
          ? "pending"
          : status === "denied"
            ? "denied"
            : status === "expired"
              ? "expired"
              : "approved";
      out.push({
        key: item.key,
        at: item.at,
        text: t(`progress.approval.${state}` as MessageKey, {
          what: item.action.target,
        }),
      });
    }
  }
  return out;
}

/** Brings an item of the conversation into view and lights it up once. */
function jumpTo(key: string) {
  const el = document.getElementById(`item-${key}`);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("anim-flash");
  void el.offsetWidth; // restart the animation
  el.classList.add("anim-flash");
}

/** The goal, to read in full, change or clear, and the task's progress. A change is a
 * /goal message, so it shows in the conversation and reaches the agent the same way as
 * one typed in the box. */
export function GoalTab({
  task,
  items,
  onChanged,
  onJump,
}: {
  task: TaskDetail;
  items: TimelineItem[];
  onChanged: () => Promise<void>;
  onJump?: () => void;
}) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const worked = useGoalTime(task);
  const [draft, setDraft] = useState<string>();
  const [clearing, setClearing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const finished = ["stopped", "completed", "failed"].includes(task.status);
  const marks = milestones(items, t);

  async function send(text: string) {
    setBusy(true);
    setError(undefined);
    try {
      await api.addInput(task.id, { text });
      await onChanged();
      setDraft(undefined);
      setClearing(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4 p-4">
      {draft === undefined ? (
        <div className="rounded-xl border border-line bg-surface p-4">
          {task.goal ? (
            <>
              {task.goal_paused && (
                <p className="mb-2 flex items-center gap-1.5 text-[12.5px] font-medium text-muted">
                  <Ico icon={Pause} size={13} />
                  {t("goal.pausedNote")}
                </p>
              )}
              <p
                className={cn(
                  "text-[14.5px] leading-relaxed break-words whitespace-pre-wrap",
                  task.goal_paused && "text-muted",
                )}
              >
                {task.goal}
              </p>
              <p className="tnum mt-3 text-[12.5px] text-faint">
                {t("goal.meta", {
                  time: duration(worked, t),
                  calls: number(task.usage.model_calls, locale),
                })}
              </p>
            </>
          ) : (
            <p className="text-[14px] text-muted">{t("goal.none")}</p>
          )}
          {!finished && (
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                size="sm"
                icon={Pencil}
                onClick={() => setDraft(task.goal)}
              >
                {t(task.goal ? "goal.edit" : "goal.set")}
              </Button>
              {task.goal && (
                <Button
                  size="sm"
                  icon={task.goal_paused ? Play : Pause}
                  busy={busy && !clearing}
                  onClick={() =>
                    void send(task.goal_paused ? "/goal resume" : "/goal pause")
                  }
                >
                  {t(task.goal_paused ? "goal.resume" : "goal.pause")}
                </Button>
              )}
              {task.goal && (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  onClick={() => setClearing(true)}
                >
                  {t("goal.clear")}
                </Button>
              )}
            </div>
          )}
        </div>
      ) : (
        <form
          className="space-y-2"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            if (draft.trim()) void send(`/goal ${draft.trim()}`);
          }}
        >
          <label htmlFor="goal-draft" className="text-[13px] font-medium">
            {t("goal.title")}
          </label>
          <textarea
            id="goal-draft"
            autoFocus
            rows={4}
            value={draft}
            placeholder={t("goal.placeholder")}
            onChange={(e) => setDraft(e.target.value)}
            className={cn(inputClass, "resize-y leading-relaxed")}
          />
          <p className="text-[12.5px] text-muted">{t("goal.note")}</p>
          <div className="flex justify-end gap-2">
            <Button size="sm" onClick={() => setDraft(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              type="submit"
              variant="primary"
              busy={busy}
              disabled={!draft.trim() || draft.trim() === task.goal}
            >
              {t("goal.save")}
            </Button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-red">
          {error}
        </p>
      )}
      {task.plan.length > 0 && (
        <section aria-labelledby="plan-title">
          <h3
            id="plan-title"
            className="mb-2 text-[13px] font-medium text-muted"
          >
            {t("plan.title")}
          </h3>
          <ol className="space-y-1">
            {task.plan.map((p, i) => (
              <li
                key={i}
                className={cn(
                  "flex items-start gap-2 text-[13.5px]",
                  p.status === "done" && "text-muted line-through",
                  p.status === "in_progress" && "font-medium",
                )}
              >
                <Ico
                  icon={
                    p.status === "done"
                      ? Check
                      : p.status === "in_progress"
                        ? CircleDot
                        : Circle
                  }
                  size={14}
                  className={cn(
                    "mt-0.5 shrink-0",
                    p.status === "in_progress" ? "text-accent" : "text-faint",
                  )}
                />
                <span className="sr-only">
                  {t(`plan.${p.status}` as MessageKey)}
                </span>
                <span className="min-w-0 flex-1">{p.step}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
      {task.notes.length > 0 && (
        <section aria-labelledby="notes-title">
          <h3
            id="notes-title"
            className="mb-1 text-[13px] font-medium text-muted"
          >
            {t("notes.title")}
          </h3>
          <p className="mb-2 text-[12px] text-faint">{t("notes.help")}</p>
          <ul className="space-y-1">
            {task.notes.map((n, i) => (
              <li key={i} className="flex items-baseline gap-2 text-[13px]">
                <span className="min-w-0 flex-1 break-words">{n.text}</span>
                {n.seen_at && (
                  <span className="tnum shrink-0 text-[12px] text-faint">
                    {clock(n.seen_at, locale)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section aria-labelledby="progress-title">
        <h3
          id="progress-title"
          className="mb-2 text-[13px] font-medium text-muted"
        >
          {t("progress.title")}
        </h3>
        {marks.length === 0 ? (
          <p className="text-[13px] text-faint">{t("progress.empty")}</p>
        ) : (
          <ol className="relative space-y-0.5 border-l border-line pl-3">
            {marks.map((m) => (
              <li key={m.key}>
                <button
                  type="button"
                  title={t("progress.jump")}
                  onClick={() => {
                    onJump?.();
                    // After a sheet has closed, the conversation is there to scroll.
                    setTimeout(() => jumpTo(m.key), onJump ? 260 : 0);
                  }}
                  className="group relative flex w-full items-baseline gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-sunken pointer-coarse:min-h-10"
                >
                  <span
                    aria-hidden
                    className="absolute top-[0.9em] -left-[17px] h-1.5 w-1.5 rounded-full bg-line-strong group-hover:bg-accent"
                  />
                  <span className="min-w-0 flex-1 truncate">{m.text}</span>
                  <span className="tnum shrink-0 text-[12px] text-faint">
                    {clock(m.at, locale)}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        )}
      </section>
      <Dialog
        open={clearing}
        onClose={() => setClearing(false)}
        title={t("goal.clear.title")}
        footer={
          <>
            <Button data-autofocus onClick={() => setClearing(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              busy={busy}
              onClick={() => void send("/goal clear")}
            >
              {t("goal.clear")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{t("goal.clear.body")}</p>
      </Dialog>
    </div>
  );
}
