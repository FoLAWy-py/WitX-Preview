import type {
  ActionView,
  EventView,
  MemoryView,
  PurchaseView,
  TaskDetail,
  TaskInputView,
  TaskResult,
} from "@/api/types.gen";
import type { MessageKey } from "@/i18n";
import { byTime, isAfter } from "@/lib/time";

/** A system line: a message from the catalog and the values it names. */
export type Notice = { message: MessageKey; vars?: Record<string, string> };

export type TimelineItem =
  | { kind: "input"; at: string; key: string; input: TaskInputView }
  | {
      kind: "message";
      at: string;
      key: string;
      text: string;
      /** The agent (run) that wrote it, when the event says. */
      runId?: string;
    }
  | {
      kind: "question";
      at: string;
      key: string;
      text: string;
      runId?: string;
      /** Answers the agent offers, and the index of the one it recommends. */
      options: string[];
      recommended: number | null;
      /** Asked of the main agent, not of you; or put to you by the main agent. */
      to: "user" | "main";
      viaMain: boolean;
      /** Not a question: why a sub-agent stopped for you (its wait), so you can tell
       * it how to go on. */
      stopped?: string;
    }
  | {
      kind: "relay";
      at: string;
      key: string;
      /** The main agent's word to a sub-agent: an answer, a message, its
       * question put to you, or a stop. */
      relay: "answer" | "message" | "ask_user" | "stop";
      toRunId: string;
      text: string;
    }
  | { kind: "plan"; at: string; key: string; text: string }
  | { kind: "skill"; at: string; key: string; draft: SkillDraft }
  | { kind: "action"; at: string; key: string; action: ActionView }
  | ({ kind: "status"; at: string; key: string } & Notice)
  | { kind: "proposal"; at: string; key: string; proposal: Proposal }
  | { kind: "purchase"; at: string; key: string; purchase: PurchaseView }
  | { kind: "memory"; at: string; key: string; memory: MemoryView }
  | {
      kind: "result";
      at: string;
      key: string;
      result: TaskResult;
      /** The newest round's result, which is the task's result while it is done. */
      latest: boolean;
      /** The round failed: the result says why. */
      failed: boolean;
    };

/** A schedule the agent proposed in the conversation; the person turns it on. */
export interface Proposal {
  schedule_id: string;
  goal: string;
  repeat: "once" | "daily" | "weekdays" | "weekly" | "monthly";
  time_of_day: string;
  weekdays: number[];
  month_day: number | null;
  run_on: string | null;
}

/** A skill the agent drafted; only the person saves it. */
export interface SkillDraft {
  /** As it will be saved: `my:<name>`. */
  name: string;
  description: string;
  apps: string[];
  body: string;
}

/** Merges the task's stored records and its event stream into one ordered list. */
export function buildTimeline(
  task: TaskDetail,
  events: EventView[],
): TimelineItem[] {
  const items: TimelineItem[] = [];
  // The note the server adds for the agent when a takeover ends or a purchase is
  // chosen (in the same transaction, so it is the very next event) is shown as that
  // takeover's line or on the purchase card.
  const takeoverNotes = new Set<unknown>();
  events.forEach((event, i) => {
    const next = events[i + 1];
    if (
      (event.type === "takeover.ended" ||
        event.type === "task.devices" ||
        event.type === "purchase.decided") &&
      next?.type === "input.added" &&
      next.seq === event.seq + 1
    )
      takeoverNotes.add(next.data.seq);
  });
  for (const input of task.inputs) {
    // The main agent's word to a sub-agent is shown with its event (agent.relay).
    // A queued message is shown at the message box until the main agent reads it.
    // A note the system wrote for the agent is shown by its own card or line.
    if (
      takeoverNotes.has(input.seq) ||
      input.kind === "agent" ||
      input.kind === "system" ||
      input.queued
    )
      continue;
    items.push({
      kind: "input",
      at: input.created_at,
      key: `input-${input.seq}`,
      input,
    });
  }
  for (const action of task.actions) {
    items.push({
      kind: "action",
      at: action.created_at,
      key: `action-${action.id}`,
      action,
    });
  }
  for (const memory of task.memories) {
    items.push({
      kind: "memory",
      // Where it was kept or last brought up to date: in this task's conversation.
      at: memory.decided_at ?? memory.created_at,
      key: `memory-${memory.id}`,
      memory,
    });
  }
  // Every finished round keeps its result where it happened, also once the task goes
  // on with a new message; a failed one says why it failed.
  const rounds = task.runs
    .filter((r) => r.parent_run_id === null && r.result)
    .sort((a, b) => byTime(a.result!.completed_at, b.result!.completed_at));
  rounds.forEach((run, i) => {
    items.push({
      kind: "result",
      at: run.result!.completed_at,
      key: `result-${run.id}`,
      result: run.result!,
      latest: i === rounds.length - 1,
      failed: run.status === "failed",
    });
  });
  // A server that does not give each round's result yet still gives the task's.
  if (task.result && rounds.length === 0)
    items.push({
      kind: "result",
      at: task.result.completed_at,
      key: "result",
      result: task.result,
      latest: true,
      failed: task.status === "failed",
    });
  for (const purchase of task.purchases) {
    items.push({
      kind: "purchase",
      at: purchase.created_at,
      key: `purchase-${purchase.id}`,
      purchase,
    });
  }
  for (const event of events) {
    const key = `event-${event.seq}`;
    const text = typeof event.data.text === "string" ? event.data.text : "";
    const runId =
      typeof event.data.run_id === "string" ? event.data.run_id : undefined;
    if (event.type === "agent.message" && text) {
      items.push({ kind: "message", at: event.created_at, key, text, runId });
    } else if (event.type === "agent.question" && text) {
      const options = Array.isArray(event.data.options)
        ? event.data.options.filter((o): o is string => typeof o === "string")
        : [];
      const recommended =
        typeof event.data.recommended === "number" &&
        event.data.recommended >= 0 &&
        event.data.recommended < options.length
          ? event.data.recommended
          : null;
      items.push({
        kind: "question",
        at: event.created_at,
        key,
        runId,
        text,
        options,
        recommended,
        to: event.data.to === "main" ? "main" : "user",
        viaMain: event.data.via === "main",
      });
    } else if (
      event.type === "agent.relay" &&
      typeof event.data.to_run_id === "string"
    ) {
      const relay = String(event.data.kind);
      if (["answer", "message", "ask_user", "stop"].includes(relay))
        items.push({
          kind: "relay",
          at: event.created_at,
          key,
          relay: relay as "answer" | "message" | "ask_user" | "stop",
          toRunId: event.data.to_run_id,
          text,
        });
    } else if (event.type === "plan.proposed" && text) {
      items.push({ kind: "plan", at: event.created_at, key, text });
    } else if (event.type === "skill.drafted") {
      items.push({
        kind: "skill",
        at: event.created_at,
        key,
        draft: event.data as unknown as SkillDraft,
      });
    } else if (event.type === "task.plan_mode") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: event.data.on
          ? "timeline.sys.planOn"
          : event.data.approved
            ? "timeline.sys.planApproved"
            : "timeline.sys.planOff",
      });
    } else if (event.type === "goal.paused" || event.type === "goal.resumed") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message:
          event.type === "goal.paused"
            ? "timeline.sys.goalPaused"
            : "timeline.sys.goalResumed",
      });
    } else if (event.type === "skill.loaded") {
      // A task's status is in its header, and its result in the result card: status
      // changes are not lines of the conversation.
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.skill",
        vars: {
          name: String(event.data.name),
          version: String(event.data.version),
        },
      });
    } else if (event.type === "task.model") {
      const fallbackFor = event.data.fallback_for;
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        ...(fallbackFor
          ? {
              message: "timeline.sys.modelFallback",
              vars: {
                name: String(event.data.model),
                previous: String(event.data.previous),
                code: String(fallbackFor),
              },
            }
          : {
              message: "timeline.sys.model",
              vars: { name: String(event.data.model) },
            }),
      });
    } else if (event.type === "takeover.ended") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.takeover",
        vars: {
          device: String(event.data.device_id),
          count: String(event.data.inputs),
        },
      });
    } else if (event.type === "agent.web_search_on") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.webSearchOn",
        vars: { reason: String(event.data.reason ?? "") },
      });
    } else if (event.type === "agent.web_search") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: event.data.query
          ? "timeline.sys.webSearch"
          : "timeline.sys.webPage",
        vars: event.data.query
          ? { query: String(event.data.query) }
          : { url: String(event.data.url ?? "") },
      });
    } else if (event.type === "schedule.proposed") {
      items.push({
        kind: "proposal",
        at: event.created_at,
        key,
        proposal: event.data as unknown as Proposal,
      });
    } else if (event.type === "goal.evaluated") {
      // A goal found met is followed by "goal.achieved", which says so.
      const verdict = String(event.data.verdict ?? "");
      if (verdict !== "met")
        items.push({
          kind: "status",
          at: event.created_at,
          key,
          message:
            verdict === "impossible"
              ? "timeline.sys.goalImpossible"
              : "timeline.sys.goalNotMet",
          vars: {
            turn: String(event.data.turn ?? 0),
            reason: String(event.data.reason ?? ""),
          },
        });
    } else if (event.type === "goal.achieved") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.goalAchieved",
        vars: {
          turns: String(event.data.turns ?? 0),
          goal: String(event.data.goal ?? ""),
        },
      });
    } else if (event.type === "task.goal" && event.data.by !== "evaluator") {
      // (When the goal ended because it was met or impossible, the lines above say so.)
      const goal = String(event.data.goal ?? "");
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        ...(goal
          ? { message: "timeline.sys.goal", vars: { goal } }
          : { message: "timeline.sys.goalCleared" }),
      });
    } else if (event.type === "task.devices") {
      const names = (key: string) =>
        (Array.isArray(event.data[key])
          ? (event.data[key] as string[])
          : []
        ).join(", ");
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.devices",
        vars: {
          added: names("added") || "—",
          removed: names("removed") || "—",
        },
      });
    } else if (event.type === "task.reasoning_effort") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.effort",
        vars: { effort: String(event.data.effort) },
      });
    } else if (event.type === "run.model_error") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        // A step tried again by itself says so; one that stopped waits for the person.
        message: event.data.retrying
          ? "timeline.sys.modelRetry"
          : "timeline.sys.modelError",
        vars: { code: String(event.data.code) },
      });
    } else if (event.type === "run.unconfirmed") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.unconfirmed",
      });
    } else if (event.type === "run.repeating") {
      items.push({
        kind: "status",
        at: event.created_at,
        key,
        message: "timeline.sys.repeating",
        vars: { count: String(event.data.count) },
      });
    }
  }
  return items.sort((a, b) => byTime(a.at, b.at) || rank(a) - rank(b));
}

/** Where items of one moment go: the agent's words before the step it then takes (a
 * message and the action of the same step are written in one transaction, at one
 * time); everything else stays in the order it came. */
function rank(item: TimelineItem): number {
  return item.kind === "action" ? 1 : 0;
}

/** The questions waiting for your answer, oldest first: for each agent that waits for
 * you, its newest question. One is answered at a time. A sub-agent that stopped
 * for you without a question (a loop, a model error), or whose question you answered
 * already, is asked how to go on, after the questions. */
export function openQuestions(
  task: TaskDetail,
  items: TimelineItem[],
): Extract<TimelineItem, { kind: "question" }>[] {
  const asked = items.filter(
    (i): i is Extract<TimelineItem, { kind: "question" }> =>
      i.kind === "question" && i.to === "user",
  );
  const open: Extract<TimelineItem, { kind: "question" }>[] = [];
  for (const run of task.runs) {
    if (run.status !== "waiting" || run.wait_reason !== "user_input") continue;
    const main = run.parent_run_id === null;
    const newest = [...asked]
      .reverse()
      .find((q) => q.runId === run.id || (main && q.runId === undefined));
    const answered = task.inputs.some(
      (i) => i.run_id === run.id && newest && isAfter(i.created_at, newest.at),
    );
    const stopped =
      !main && (!newest || answered || stopReason(run.wait_detail));
    if (stopped)
      open.push({
        kind: "question",
        at: "9999",
        key: `stopped-${run.id}`,
        runId: run.id,
        text: "",
        options: [],
        recommended: null,
        to: "user",
        viaMain: false,
        stopped: run.wait_detail ?? "",
      });
    else if (newest) open.push(newest);
  }
  return open.sort((a, b) => byTime(a.at, b.at));
}

/** Why a run stopped for you when it did not ask (as the server writes it), else "". */
export function stopReason(
  detail: string | null,
): "" | "repeating" | "unconfirmed" | "modelError" {
  if (detail === "repeating" || detail === "unconfirmed") return detail;
  return detail?.startsWith("model error:") ? "modelError" : "";
}

/** The newest complete screenshot of each device. */
export function latestScreens(task: TaskDetail): Map<string, string> {
  const latest = new Map<string, { id: string; at: string }>();
  for (const obs of task.observations) {
    if (obs.status !== "complete" || !obs.captured_at) continue;
    const current = latest.get(obs.device_id);
    if (!current || isAfter(obs.captured_at, current.at)) {
      latest.set(obs.device_id, { id: obs.id, at: obs.captured_at });
    }
  }
  return new Map([...latest].map(([device, value]) => [device, value.id]));
}

/** A screenshot taken after the action finished, which a user may cite when settling it. */
export function screenshotAfter(
  task: TaskDetail,
  action: ActionView,
): string | undefined {
  const after = task.observations
    .filter(
      (o) =>
        o.device_id === action.device_id &&
        o.status === "complete" &&
        o.captured_at !== null &&
        isAfter(o.captured_at, action.created_at),
    )
    .sort((a, b) => byTime(b.captured_at ?? "", a.captured_at ?? ""));
  return after[0]?.id;
}

/** What the agent itself did along the way, besides its steps: lines of its round
 *. Everything else that changes the task (a model, a goal, plan mode, a takeover,
 * phones) stays a divider of the conversation. */
const WORK_NOTICES: ReadonlySet<MessageKey> = new Set<MessageKey>([
  "timeline.sys.webSearchOn",
  "timeline.sys.webSearch",
  "timeline.sys.webPage",
  "timeline.sys.skill",
  "timeline.sys.repeating",
]);

export type WorkItem = Extract<TimelineItem, { kind: "action" | "status" }>;

export type Block =
  | { kind: "item"; item: TimelineItem }
  | { kind: "round"; key: string; items: WorkItem[] };

/** The conversation's blocks: consecutive steps, and what the agent did between them,
 * fold into one round; steps that need the person stand on their own, and so does
 * everything said. */
export function blocks(items: TimelineItem[]): Block[] {
  const out: Block[] = [];
  for (const item of items) {
    // A decision the person made stays in sight as its own line, not folded away.
    const work =
      (item.kind === "action" &&
        item.action.status !== "awaiting_approval" &&
        !item.action.approval &&
        !(item.action.status === "done" && item.action.effect === "unknown")) ||
      (item.kind === "status" && WORK_NOTICES.has(item.message));
    const last = out[out.length - 1];
    if (work && last?.kind === "round") last.items.push(item as WorkItem);
    else if (work)
      out.push({ kind: "round", key: item.key, items: [item as WorkItem] });
    else out.push({ kind: "item", item });
  }
  return out;
}
