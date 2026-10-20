import { useEffect, useState } from "react";
import type {
  DeviceView,
  RunView,
  TaskDetail,
  SayingView,
} from "@/api/types.gen";
import { useI18n, type Translate } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn, duration } from "@/lib/format";
import { kindKey } from "@/lib/effects";
import {
  blocks,
  openQuestions,
  stopReason,
  type TimelineItem,
} from "@/lib/timeline";
import { agentName, nameDevices, runLabel } from "@/lib/agents";
import { ApprovalCard } from "./Approval";
import { PlanCard } from "./PlanCard";
import { SkillDraftCard } from "./Skills";
import { QuestionAnswer } from "./QuestionAnswer";
import { Rich } from "./Rich";
import { ProposalCard } from "./Schedule";
import { PurchaseCard } from "./Purchase";
import { MemoryCard } from "./Memory";
import { MessageActions } from "./MessageActions";
import { sayingOf } from "@/lib/saying";
import { advance } from "@/lib/typewriter";
import { Round } from "./Steps";
import { UnknownCard, ApprovalRecord } from "./Cards";
import { Evidence } from "./Evidence";

/** Whether an agent of the task is waiting for the person to answer its question. */
export function awaitingAnswer(task: TaskDetail): boolean {
  return task.runs.some(
    (r) => r.status === "waiting" && r.wait_reason === "user_input",
  );
}

/** Whether the main agent waits for an answer: then the message box answers it. A
 * sub-agent's question is answered in its card. */
export function mainAsks(task: TaskDetail): boolean {
  return task.runs.some(
    (r) =>
      r.parent_run_id === null &&
      r.status === "waiting" &&
      r.wait_reason === "user_input",
  );
}

/** What the agent is doing right now, in words: the input under way on which phone,
 * looking at a screen, or what its step is at: writing, planning the next
 * input, having its risk assessed, having its summary checked, working on after the
 * check refused the summary, or just thinking; thinking after it searched the web or
 * loaded a skill, that (`notice`, D73). */
function activity(
  task: TaskDetail,
  run: RunView | undefined,
  devices: DeviceView[],
  saying: SayingView | null,
  t: Translate,
  notice?: string,
): { label: string; thinking: boolean } {
  const name = (id: string) =>
    devices.find((d) => d.id === id)?.name ?? t("common.device");
  const busy = (label: string) => ({ label, thinking: false });
  if (run?.status === "waiting" && run.wait_reason)
    return busy(runLabel(run, devices, t));
  const doing = [...task.actions]
    .reverse()
    .find((a) => a.status === "queued" || a.status === "sent");
  if (doing)
    return busy(
      t("activity.doing", {
        kind: t(kindKey(doing)),
        target: doing.target,
        device: name(doing.device_id),
      }),
    );
  const looking = task.observations.find(
    (o) => o.status === "requested" || o.status === "sent",
  );
  if (looking)
    return busy(t("activity.looking", { device: name(looking.device_id) }));
  const phase = saying && run && saying.run_id === run.id ? saying.phase : null;
  if (notice && !phase) return { label: notice, thinking: true };
  return { label: t(`activity.${phase ?? "thinking"}`), thinking: true };
}

/** What the agent does, and for how long once it has been at it a moment. The count is
 * not read out; only a change of what it does is. */
function Activity({
  task,
  run,
  devices,
  saying,
  notice,
}: {
  task: TaskDetail;
  run: RunView | undefined;
  devices: DeviceView[];
  saying: SayingView | null;
  notice?: string;
}) {
  const { t } = useI18n();
  const { label, thinking } = activity(task, run, devices, saying, t, notice);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  // How long this turn has been going: since the person last spoke, whatever the agent
  // has been doing since (a label that changes does not restart it).
  const elapsed = Math.max(0, now - turnStart(task));
  return (
    <>
      <span
        aria-live="polite"
        className={cn("min-w-0 truncate", thinking && "anim-shimmer")}
      >
        {label}
      </span>
      {elapsed >= 2000 && (
        <span aria-hidden className="tnum shrink-0 font-normal text-faint">
          {duration(elapsed, t)}
        </span>
      )}
    </>
  );
}

/** The words to show on the way to `target`, catching up at an even pace. */
function useTypewriter(target: string | null): string {
  const [shown, setShown] = useState("");
  useEffect(() => {
    if (!target) {
      setShown("");
      return;
    }
    let frame = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      setShown((current) => {
        const next = advance(current, target, dt);
        if (next !== target) frame = requestAnimationFrame(tick);
        return next;
      });
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target]);
  return target ? shown : "";
}

/** When the turn under way began: the person's latest message, or the task's start. */
function turnStart(task: TaskDetail): number {
  const spoken = task.inputs.filter(
    (i) => i.kind !== "agent" && i.kind !== "system" && !i.queued,
  );
  const last = spoken[spoken.length - 1];
  return Date.parse(last?.created_at ?? task.created_at);
}

/** The live line while the agent works: what it does, and for how long this turn has
 * been going. Always the last thing, in the same place, whatever grows above it: the
 * line of the round under way when that is the last thing, else its own line. */
function Working(props: {
  task: TaskDetail;
  run: RunView | undefined;
  devices: DeviceView[];
  saying: SayingView | null;
  notice?: string;
}) {
  return (
    <span className="flex min-w-0 items-center gap-3">
      <span className="relative flex h-6 w-6 shrink-0 items-center justify-center">
        <span className="anim-pulse h-2 w-2 rounded-full bg-accent" />
      </span>
      <Activity {...props} />
    </span>
  );
}

/** The task as a conversation: what you asked, what the agent said and did. */
export function Conversation({
  task,
  items,
  devices,
  saying = null,
  onChange,
  onTakeOver,
  onEdit,
  scope,
}: {
  task: TaskDetail;
  items: TimelineItem[];
  devices: DeviceView[];
  /** What the agent is saying right now, as the model produces it. */
  saying?: SayingView | null;
  onChange: () => Promise<void>;
  onTakeOver: (deviceId: string) => void;
  /** Puts a message's text into the message box, to be edited and sent again. */
  onEdit?: (text: string) => void;
  /** Only this agent's part of the task (its tab in the side panel). */
  scope?: RunView;
}) {
  const { t } = useI18n();
  const { me } = useAuth();
  // Scoped to one agent (its tab in the side panel), "live" and "what it does now" are
  // that agent's; otherwise the task's and its main agent's.
  const root = scope ?? task.runs.find((r) => r.parent_run_id === null);
  const live = scope
    ? task.status === "active" &&
      ["runnable", "running", "waiting"].includes(scope.status)
    : task.status === "active";
  // Who wrote a line: only a sub-agent is named; the main agent is the task's voice.
  const subAgent = (runId?: string) => {
    const run = task.runs.find((r) => r.id === runId);
    return run?.parent_run_id && run.id !== scope?.id
      ? agentName(run, task.runs, devices, t)
      : undefined;
  };
  // Waiting for your answer: the agent shown (a sub-agent's question has its card).
  const asking =
    root?.status === "waiting" && root.wait_reason === "user_input";
  // Waiting for an approval: the card above says so, with what to decide.
  const approving =
    root?.status === "waiting" && root.wait_reason === "approval";
  // The questions still waiting for an answer, one per agent, answered one at a time
  // (the oldest first); in an agent's tab, its own.
  const queue = openQuestions(task, items);
  const current = queue[0]?.key;
  const place = (key: string) => queue.findIndex((q) => q.key === key);
  const runOf = (runId?: string) => task.runs.find((r) => r.id === runId);
  // The plan still waiting for approval: the newest one, while the task is in plan mode.
  const openPlan = task.plan_mode
    ? [...items].reverse().find((i) => i.kind === "plan")?.key
    : undefined;
  const device = (id: string) => devices.find((d) => d.id === id);
  // A sub-agent that stopped for you is asked here, after everything else.
  const list = blocks([
    ...items,
    ...queue.filter((q) => q.stopped !== undefined),
  ]);
  const working = live && !asking && !approving;
  const words = live ? sayingOf(saying, root, items) : null;
  const shown = useTypewriter(words);
  const refused = words !== null && saying?.phase === "rejected";
  // The live line is the line of the round under way while that round is the last
  // thing; after the agent's words in the making, or anything else, it is its own line.
  const last = list[list.length - 1];
  const inRound = working && !shown && last?.kind === "round";
  const newest = last?.kind === "round" ? last.items.at(-1) : undefined;
  const notice =
    newest?.kind === "status" ? t(newest.message, newest.vars) : undefined;
  return (
    <ol aria-label={t("conversation.label")} className="space-y-5">
      {list.map((block, index) => {
        if (block.kind === "round") {
          return (
            <li
              key={block.key}
              id={`item-${block.key}`}
              className="anim-slide scroll-mt-4"
            >
              <Round
                task={task}
                items={block.items}
                devices={devices}
                live={
                  inRound && index === list.length - 1 ? (
                    <Working
                      task={task}
                      run={root}
                      devices={devices}
                      saying={saying}
                      notice={notice}
                    />
                  ) : undefined
                }
              />
            </li>
          );
        }
        const item = block.item;
        switch (item.kind) {
          case "input":
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="group/msg anim-slide flex scroll-mt-4 flex-col items-end"
              >
                <div className="max-w-[85%] rounded-2xl rounded-br-md bg-fill px-4 py-2.5 text-[15px] leading-relaxed">
                  {item.input.kind === "constraint" && (
                    <div className="mb-0.5 text-[12px] font-medium text-accent">
                      {t("timeline.constraint")}
                    </div>
                  )}
                  {subAgent(item.input.run_id ?? undefined) && (
                    <div className="mb-0.5 text-[12px] font-medium text-muted">
                      {t("timeline.replyTo", {
                        name: subAgent(item.input.run_id ?? undefined)!,
                      })}
                    </div>
                  )}
                  <span className="whitespace-pre-wrap break-words">
                    {item.input.text}
                  </span>
                </div>
                <MessageActions
                  task={task}
                  input={item.input}
                  onEdit={onEdit}
                  onChange={onChange}
                />
              </li>
            );
          case "relay": {
            const to = runOf(item.toRunId);
            const name = to ? agentName(to, task.runs, devices, t) : "";
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="anim-slide scroll-mt-4 pl-9 text-[13px] leading-relaxed text-muted"
              >
                <span className="font-medium">
                  {t(`timeline.relay.${item.relay}`, { name })}
                </span>
                {item.text && (
                  <span className="whitespace-pre-wrap break-words">
                    {" "}
                    {nameDevices(item.text, devices)}
                  </span>
                )}
              </li>
            );
          }
          case "message":
          case "question":
            if (item.kind === "question" && item.to === "main")
              return (
                <li
                  key={item.key}
                  id={`item-${item.key}`}
                  className="anim-slide scroll-mt-4 pl-9 text-[13px] leading-relaxed text-muted"
                >
                  <span className="font-medium">
                    {t("timeline.askedMain", {
                      name:
                        subAgent(item.runId) ??
                        (runOf(item.runId)
                          ? agentName(runOf(item.runId)!, task.runs, devices, t)
                          : t("panel.mainAgent")),
                    })}
                  </span>{" "}
                  <span className="whitespace-pre-wrap break-words">
                    {nameDevices(item.text, devices)}
                  </span>
                </li>
              );
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="anim-slide flex scroll-mt-4 gap-3"
              >
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
                >
                  W
                </span>
                <div
                  className={cn(
                    "min-w-0 flex-1 text-[15px] leading-relaxed",
                    item.kind === "question" &&
                      "rounded-xl border border-amber/40 bg-amber-soft/60 px-4 py-3",
                  )}
                >
                  {item.kind === "question" ? (
                    <div className="mb-1 flex items-baseline gap-2 text-[12.5px] font-medium text-amber">
                      <span className="min-w-0 flex-1">
                        {subAgent(item.runId)
                          ? t(
                              item.viaMain
                                ? "timeline.questionByVia"
                                : "timeline.questionBy",
                              { name: subAgent(item.runId)! },
                            )
                          : t("timeline.question")}
                      </span>
                      {place(item.key) >= 0 && queue.length > 1 && (
                        <span className="shrink-0 tabular-nums text-amber/80">
                          {t("question.place", {
                            n: String(place(item.key) + 1),
                            count: String(queue.length),
                          })}
                        </span>
                      )}
                    </div>
                  ) : (
                    subAgent(item.runId) && (
                      <div className="mb-0.5 text-[12.5px] font-medium text-muted">
                        {subAgent(item.runId)}
                      </div>
                    )
                  )}
                  {item.kind === "question" && item.stopped !== undefined ? (
                    <p>
                      {t(
                        `question.stopped.${stopReason(item.stopped) || "other"}`,
                      )}
                    </p>
                  ) : (
                    <Rich text={nameDevices(item.text, devices)} />
                  )}
                  {item.kind === "question" &&
                    (item.key === current ? (
                      <QuestionAnswer
                        task={task}
                        runId={
                          runOf(item.runId)?.parent_run_id
                            ? item.runId
                            : undefined
                        }
                        options={item.options}
                        recommended={item.recommended}
                        onChange={onChange}
                      />
                    ) : (
                      place(item.key) > 0 && (
                        <p className="mt-2 text-[12.5px] text-muted">
                          {t("question.queued")}
                        </p>
                      )
                    ))}
                </div>
              </li>
            );
          case "plan":
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="anim-slide flex scroll-mt-4 gap-3"
              >
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
                >
                  W
                </span>
                <div className="min-w-0 flex-1">
                  <PlanCard
                    task={task}
                    text={item.text}
                    open={item.key === openPlan}
                    onChange={onChange}
                  />
                </div>
              </li>
            );
          case "skill":
            return (
              <li key={item.key} className="anim-slide flex gap-3">
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
                >
                  W
                </span>
                <div className="min-w-0 flex-1">
                  <SkillDraftCard draft={item.draft} task={task} />
                </div>
              </li>
            );
          case "proposal":
            return (
              <li key={item.key} className="anim-slide flex gap-3">
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
                >
                  W
                </span>
                <div className="min-w-0 flex-1">
                  <ProposalCard proposal={item.proposal} />
                </div>
              </li>
            );
          case "memory":
            // Someone's memory is theirs: others viewing the task do not see it.
            return task.created_by && task.created_by !== me?.user_id ? null : (
              <li key={item.key} className="anim-slide">
                <MemoryCard memory={item.memory} onChange={onChange} />
              </li>
            );
          case "purchase":
            return (
              <li key={item.key} className="anim-slide flex gap-3">
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
                >
                  W
                </span>
                <div className="min-w-0 flex-1">
                  <PurchaseCard
                    task={task}
                    purchase={item.purchase}
                    devices={devices}
                    onChange={onChange}
                  />
                </div>
              </li>
            );
          case "result":
            // What a round ended with is the agent's own words, as a message, with the
            // screenshots it rests on; a round that failed says why in red.
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="anim-slide flex scroll-mt-4 gap-3"
              >
                <span
                  aria-hidden
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
                >
                  W
                </span>
                <div
                  className={cn(
                    "min-w-0 flex-1 text-[15px] leading-relaxed",
                    item.failed && "text-red",
                  )}
                >
                  {item.result.failure ? (
                    <p>{t(`result.failure.${item.result.failure}`)}</p>
                  ) : (
                    <Rich text={nameDevices(item.result.summary, devices)} />
                  )}
                  <Evidence
                    task={task}
                    ids={item.result.evidence_observation_ids}
                    devices={devices}
                  />
                </div>
              </li>
            );
          case "status":
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="flex scroll-mt-4 items-center gap-2 text-[12.5px] text-faint"
              >
                <span className="h-px flex-1 bg-line" />
                {t(item.message, {
                  ...item.vars,
                  // A device named by id is shown by its name.
                  ...(item.vars?.device && {
                    device:
                      devices.find((d) => d.id === item.vars?.device)?.name ??
                      t("common.device"),
                  }),
                })}
                <span className="h-px flex-1 bg-line" />
              </li>
            );
          case "action": {
            const a = item.action;
            const unknown = a.status === "done" && a.effect === "unknown";
            return (
              <li
                key={item.key}
                id={`item-${item.key}`}
                className="anim-slide scroll-mt-4"
              >
                {a.status === "awaiting_approval" ? (
                  <ApprovalCard
                    task={task}
                    action={a}
                    device={device(a.device_id)}
                    onChange={onChange}
                  />
                ) : a.approval && !unknown ? (
                  <ApprovalRecord
                    task={task}
                    action={a}
                    device={device(a.device_id)}
                    onChange={onChange}
                  />
                ) : (
                  <UnknownCard
                    task={task}
                    action={a}
                    device={device(a.device_id)}
                    onChange={onChange}
                    onTakeOver={
                      ["active", "paused"].includes(task.status)
                        ? () => onTakeOver(a.device_id)
                        : undefined
                    }
                  />
                )}
              </li>
            );
          }
        }
        return null;
      })}
      {shown && (
        // The agent's words as they come: a message in the making, in the same
        // bubble its message will have once the step is over, at an even pace. A
        // summary the check refused stays, faded, until the agent says something new.
        <li
          id="item-saying"
          aria-live="polite"
          data-refused={refused || undefined}
          className={cn(
            "anim-slide flex scroll-mt-4 gap-3",
            refused && "opacity-50",
          )}
        >
          <span
            aria-hidden
            className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-fg text-[11px] font-semibold text-bg"
          >
            W
          </span>
          <div className="min-w-0 flex-1 text-[15px] leading-relaxed">
            <Rich text={nameDevices(shown, devices)} />
          </div>
        </li>
      )}
      {working && !inRound && (
        <li className="py-1 text-[13.5px] text-muted">
          <Working task={task} run={root} devices={devices} saying={saying} />
        </li>
      )}
    </ol>
  );
}
