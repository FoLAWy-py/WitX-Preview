import { describe, expect, it } from "vitest";
import type { ActionView, TaskDetail } from "@/api/types.gen";
import {
  blocks,
  buildTimeline,
  latestScreens,
  openQuestions,
  screenshotAfter,
  type TimelineItem,
} from "./timeline";

const task: TaskDetail = {
  id: "t",
  request: "g",
  goal: "",
  goal_paused: false,
  plan_mode: false,
  agent_ms: 0,
  goal_ms: 0,
  working: false,
  title: null,
  constraints: [],
  device_ids: [],
  status: "active",
  revision: 1,
  model: "m",
  reasoning_effort: "medium",
  permission_mode: "auto",
  usage: { model_calls: 0, input_tokens: 0, output_tokens: 0 },
  result: null,
  inputs: [
    { seq: 1, kind: "goal", text: "g", created_at: "2026-10-10T10:00:00Z" },
  ],
  runs: [],
  actions: [],
  grants: [],
  purchases: [],
  plan: [],
  notes: [],
  memories: [],
  observations: [
    obs("o1", "d1", "2026-10-10T10:00:05Z"),
    obs("o2", "d1", "2026-10-10T10:00:09Z"),
    obs("o3", "d2", "2026-10-10T10:00:07Z"),
  ],
  last_event_seq: 0,
  created_at: "2026-10-10T10:00:00Z",
  updated_at: "2026-10-10T10:00:00Z",
};

function obs(
  id: string,
  device: string,
  at: string,
): TaskDetail["observations"][number] {
  return {
    id,
    device_id: device,
    status: "complete",
    captured_at: at,
    width: 1,
    height: 1,
    foreground_app: "a",
  };
}

describe("timeline", () => {
  it("shows what the agent searched on the web", () => {
    const items = buildTimeline(task, [
      {
        seq: 1,
        type: "agent.web_search",
        data: { query: "999感冒灵 价格" },
        created_at: "2026-10-10T10:00:01Z",
      },
    ]);
    expect(
      items
        .filter((i) => i.kind === "status")
        .map((i) => i.kind === "status" && [i.message, i.vars]),
    ).toEqual([["timeline.sys.webSearch", { query: "999感冒灵 价格" }]]);
  });

  it("shows a goal check that finds the goal not met, and when the goal is achieved, but not the clearing it ends with", () => {
    const at = "2026-10-10T10:00:03Z";
    const items = buildTimeline(task, [
      {
        seq: 1,
        type: "goal.evaluated",
        data: { verdict: "not_met", reason: "switch is off", turn: 1 },
        created_at: at,
      },
      {
        seq: 2,
        type: "goal.evaluated",
        data: { verdict: "met", reason: "", turn: 2 },
        created_at: at,
      },
      {
        seq: 3,
        type: "goal.achieved",
        data: { goal: "Wi-Fi open", turns: 2, reason: "" },
        created_at: at,
      },
      {
        seq: 4,
        type: "task.goal",
        data: { goal: "", previous: "Wi-Fi open", by: "evaluator" },
        created_at: at,
      },
    ]);
    expect(
      items.map((i) => (i.kind === "status" ? [i.message, i.vars] : i.kind)),
    ).toEqual([
      "input",
      ["timeline.sys.goalNotMet", { turn: "1", reason: "switch is off" }],
      ["timeline.sys.goalAchieved", { turns: "2", goal: "Wi-Fi open" }],
    ]);
  });

  it("shows a plan the agent presents as a plan, and plan mode changes as lines", () => {
    const at = "2026-10-10T10:00:03Z";
    const items = buildTimeline(task, [
      {
        seq: 1,
        type: "task.plan_mode",
        data: { on: true, by: "user" },
        created_at: at,
      },
      {
        seq: 2,
        type: "plan.proposed",
        data: { text: "1. Open Settings" },
        created_at: at,
      },
      {
        seq: 3,
        type: "task.plan_mode",
        data: { on: false, approved: true },
        created_at: at,
      },
      { seq: 4, type: "task.plan_mode", data: { on: false }, created_at: at },
    ]);
    expect(
      items.map((i) =>
        i.kind === "status" ? i.message : i.kind === "plan" ? i.text : i.kind,
      ),
    ).toEqual([
      "input",
      "timeline.sys.planOn",
      "1. Open Settings",
      "timeline.sys.planApproved",
      "timeline.sys.planOff",
    ]);
  });

  it("shows the note a takeover leaves for the agent as a line about the takeover, not as your message", () => {
    const at = "2026-10-10T10:00:03Z";
    const items = buildTimeline(
      {
        ...task,
        inputs: [
          ...task.inputs,
          {
            seq: 2,
            kind: "note",
            text: "I controlled device d1 myself",
            created_at: at,
          },
        ],
      },
      [
        {
          seq: 7,
          type: "takeover.ended",
          data: { device_id: "d1", inputs: 3, reason: "ended" },
          created_at: at,
        },
        {
          seq: 8,
          type: "input.added",
          data: { seq: 2, kind: "note" },
          created_at: at,
        },
      ],
    );
    expect(
      items.map((i) => (i.kind === "status" ? [i.message, i.vars] : i.kind)),
    ).toEqual([
      "input",
      ["timeline.sys.takeover", { device: "d1", count: "3" }],
    ]);
  });

  it("shows a purchase choice on its card, not the note it leaves for the agent", () => {
    const at = "2026-10-10T10:00:03Z";
    const purchase = {
      id: "p1",
      goal: "Cat food",
      options: [],
      status: "chosen" as const,
      chosen: 0,
      decided_at: at,
      created_at: "2026-10-10T10:00:02Z",
    };
    const items = buildTimeline(
      {
        ...task,
        purchases: [purchase],
        inputs: [
          ...task.inputs,
          {
            seq: 2,
            kind: "note",
            text: "For 'Cat food' I chose option 1",
            created_at: at,
          },
        ],
      },
      [
        {
          seq: 7,
          type: "purchase.decided",
          data: { purchase_id: "p1", chosen: 0 },
          created_at: at,
        },
        {
          seq: 8,
          type: "input.added",
          data: { seq: 2, kind: "note" },
          created_at: at,
        },
      ],
    );
    expect(items.map((i) => i.kind)).toEqual(["input", "purchase"]);
  });

  it("says why the model changed when the task fell back on its own", () => {
    const at = "2026-10-10T10:00:03Z";
    const items = buildTimeline(task, [
      {
        seq: 3,
        type: "task.model",
        data: { model: "flash", previous: "luna", fallback_for: "http_429" },
        created_at: at,
      },
      {
        seq: 4,
        type: "task.model",
        data: { model: "luna", previous: "flash" },
        created_at: at,
      },
    ]);
    expect(
      items.flatMap((i) => (i.kind === "status" ? [[i.message, i.vars]] : [])),
    ).toEqual([
      [
        "timeline.sys.modelFallback",
        { name: "flash", previous: "luna", code: "http_429" },
      ],
      ["timeline.sys.model", { name: "luna" }],
    ]);
  });

  it("orders inputs and agent messages by time", () => {
    const items = buildTimeline(task, [
      {
        seq: 1,
        type: "agent.message",
        data: { text: "hi" },
        created_at: "2026-10-10T10:00:01Z",
      },
      {
        seq: 2,
        type: "observation.updated",
        data: {},
        created_at: "2026-10-10T10:00:02Z",
      },
    ]);
    expect(items.map((i) => i.kind)).toEqual(["input", "message"]);
  });

  it("puts the agent's words before the step it takes at the same moment", () => {
    const at = "2026-10-10T10:00:05Z";
    const items = buildTimeline(
      { ...task, actions: [{ ...task.actions[0], created_at: at }] },
      [
        {
          seq: 1,
          type: "agent.message",
          data: { text: "Going home first" },
          created_at: at,
        },
      ],
    );
    expect(items.map((i) => i.kind)).toEqual(["input", "message", "action"]);
  });

  it("orders by the moment, not by how the time is written", () => {
    // The first message came in the server's zone, the result in UTC (#140 follow-up).
    const items = buildTimeline(
      {
        ...task,
        inputs: [
          { ...task.inputs[0], created_at: "2026-10-10T18:00:00+08:00" },
        ],
        result: {
          summary: "done",
          evidence_observation_ids: [],
          completed_at: "2026-10-10T10:00:30Z",
        },
      },
      [],
    );
    expect(items.map((i) => i.kind)).toEqual(["input", "result"]);
  });

  it("says which skill the agent loaded and when the model was switched", () => {
    const items = buildTimeline(task, [
      {
        seq: 1,
        type: "skill.loaded",
        data: { name: "test-objects", version: "1.0" },
        created_at: "2026-10-10T10:00:01Z",
      },
      {
        seq: 2,
        type: "task.model",
        data: { model: "deepseek-v4.1-flash", previous: "gpt-6-luna" },
        created_at: "2026-10-10T10:00:02Z",
      },
    ]);
    expect(
      items
        .slice(1)
        .map((i) => (i.kind === "status" ? [i.message, i.vars] : null)),
    ).toEqual([
      ["timeline.sys.skill", { name: "test-objects", version: "1.0" }],
      ["timeline.sys.model", { name: "deepseek-v4.1-flash" }],
    ]);
  });

  it("offers to save only a skill's newest draft, each skill on its own", () => {
    const drafted = (seq: number, name: string) => ({
      seq,
      type: "skill.drafted",
      data: { name, description: "d", apps: [], body: "b" },
      created_at: `2026-10-10T10:00:0${seq}Z`,
    });
    const items = buildTimeline(task, [
      drafted(1, "my:open-settings"),
      drafted(2, "my:open-wifi"),
      drafted(3, "my:open-wifi"),
    ]);
    expect(
      items.flatMap((i) =>
        i.kind === "skill" ? [[i.draft.name, i.superseded]] : [],
      ),
    ).toEqual([
      ["my:open-settings", false],
      ["my:open-wifi", true],
      ["my:open-wifi", false],
    ]);
  });

  it("finds the newest screenshot per device", () => {
    expect(latestScreens(task)).toEqual(
      new Map([
        ["d1", "o2"],
        ["d2", "o3"],
      ]),
    );
  });

  it("only offers screenshots taken after an action as evidence", () => {
    const action = {
      device_id: "d1",
      created_at: "2026-10-10T10:00:06Z",
    } as ActionView;
    expect(screenshotAfter(task, action)).toBe("o2");
    expect(
      screenshotAfter(task, { ...action, created_at: "2026-10-10T10:00:10Z" }),
    ).toBeUndefined();
  });

  it("folds steps and what the agent did between them into one round; the person's changes stay dividers", () => {
    const step = (
      key: string,
      extra: Partial<ActionView> = {},
    ): TimelineItem => ({
      kind: "action",
      at: "2026-10-10T10:00:00Z",
      key,
      action: {
        status: "done",
        effect: "unverified",
        approval: null,
        ...extra,
      } as ActionView,
    });
    const notice = (key: string, message: string): TimelineItem =>
      ({
        kind: "status",
        at: "2026-10-10T10:00:00Z",
        key,
        message,
      }) as TimelineItem;
    const said: TimelineItem = {
      kind: "message",
      at: "2026-10-10T10:00:00Z",
      key: "m",
      text: "hi",
    };
    const shape = blocks([
      step("a1"),
      notice("w", "timeline.sys.webSearch"),
      step("a2"),
      notice("model", "timeline.sys.model"),
      step("a3"),
      said,
      notice("skill", "timeline.sys.skill"),
      step("a4", { status: "awaiting_approval" }),
    ]).map((b) =>
      b.kind === "round" ? b.items.map((i) => i.key).join("+") : b.item.key,
    );
    expect(shape).toEqual(["a1+w+a2", "model", "a3", "m", "skill", "a4"]);
  });

  it("keeps each finished round's result, the newest marked as the task's", () => {
    const result = (summary: string, at: string) => ({
      summary,
      evidence_observation_ids: [],
      completed_at: at,
    });
    const items = buildTimeline(
      {
        ...task,
        runs: [
          {
            ...task.runs[0],
            id: "r1",
            parent_run_id: null,
            result: result("first", "2026-10-10T10:01:00Z"),
          },
          {
            ...task.runs[0],
            id: "r2",
            parent_run_id: null,
            result: result("second", "2026-10-10T10:05:00Z"),
          },
          {
            ...task.runs[0],
            id: "c1",
            parent_run_id: "r2",
            result: result("child", "2026-10-10T10:04:00Z"),
          },
        ],
      },
      [],
    ).filter((i) => i.kind === "result");
    expect(items.map((i) => [i.result.summary, i.latest])).toEqual([
      ["first", false],
      ["second", true],
    ]);
  });

  it("ends a failed round with its result, marked failed", () => {
    const result = {
      summary:
        "The agent reached the step limit (200 steps) without finishing.",
      evidence_observation_ids: [],
      completed_at: "2026-10-10T10:05:00Z",
      failure: "step_limit" as const,
    };
    const items = buildTimeline(
      {
        ...task,
        status: "failed",
        result,
        runs: [
          {
            ...task.runs[0],
            id: "r1",
            parent_run_id: null,
            status: "failed",
            result,
          },
        ],
      },
      [],
    );
    const last = items[items.length - 1];
    expect(last.kind === "result" && [last.failed, last.latest]).toEqual([
      true,
      true,
    ]);
  });

  it("shows the task's result when no round carries one", () => {
    const result = {
      summary: "done",
      evidence_observation_ids: [],
      completed_at: "2026-10-10T10:05:00Z",
    };
    const items = buildTimeline({ ...task, result }, []).filter(
      (i) => i.kind === "result",
    );
    expect(items.map((i) => [i.result.summary, i.latest])).toEqual([
      ["done", true],
    ]);
  });

  it("queues the waiting agents' newest questions for you, oldest first, not those for the main agent", () => {
    const run = (id: string, parent: string | null, wait: string) => ({
      id,
      parent_run_id: parent,
      goal: id,
      status: "waiting" as const,
      wait_reason: wait as "user_input",
      wait_detail: null,
      device_ids: [],
      created_at: "2026-10-10T10:00:00Z",
    });
    const asked = (seq: number, runId: string, text: string, to = "user") => ({
      seq,
      type: "agent.question",
      data: { run_id: runId, text, to },
      created_at: `2026-10-10T10:00:0${seq}Z`,
    });
    const withRuns = {
      ...task,
      runs: [
        run("main", null, "children"),
        run("a", "main", "user_input"),
        run("b", "main", "user_input"),
        run("c", "main", "agent"),
      ],
    };
    const events = [
      asked(1, "a", "old question of A"),
      asked(2, "b", "B asks"),
      asked(3, "a", "A asks"),
      asked(4, "c", "C asks the main agent", "main"),
    ];
    const queue = openQuestions(withRuns, buildTimeline(withRuns, events));
    expect(queue.map((q) => q.text)).toEqual(["B asks", "A asks"]);
  });

  it("shows the main agent's word to a sub-agent once, as its line, not as your message", () => {
    const at = "2026-10-10T10:00:03Z";
    const items = buildTimeline(
      {
        ...task,
        inputs: [
          ...task.inputs,
          {
            seq: 2,
            kind: "agent",
            text: "An iPhone",
            created_at: at,
            run_id: "a",
          },
        ],
      },
      [
        {
          seq: 5,
          type: "agent.relay",
          data: {
            run_id: "main",
            to_run_id: "a",
            kind: "answer",
            text: "An iPhone",
          },
          created_at: at,
        },
      ],
    );
    expect(
      items.map((i) =>
        i.kind === "relay" ? [i.relay, i.toRunId, i.text] : i.kind,
      ),
    ).toEqual(["input", ["answer", "a", "An iPhone"]]);
  });

  it("asks a sub-agent that stopped for you, or whose question you answered, how to go on", () => {
    const run = (id: string, detail: string) => ({
      id,
      parent_run_id: "main",
      goal: id,
      status: "waiting" as const,
      wait_reason: "user_input" as const,
      wait_detail: detail,
      device_ids: [],
      created_at: "2026-10-10T10:00:00Z",
    });
    const withRuns = {
      ...task,
      inputs: [
        ...task.inputs,
        {
          seq: 2,
          kind: "note" as const,
          text: "ok",
          created_at: "2026-10-10T10:00:05Z",
          run_id: "b",
        },
      ],
      runs: [run("a", "repeating"), run("b", "Which account?")],
    };
    const events = [
      {
        seq: 1,
        type: "agent.question",
        data: { run_id: "b", text: "Which account?" },
        created_at: "2026-10-10T10:00:02Z",
      },
    ];
    const queue = openQuestions(withRuns, buildTimeline(withRuns, events));
    expect(queue.map((q) => [q.runId, q.stopped])).toEqual([
      ["a", "repeating"],
      ["b", "Which account?"],
    ]);
  });
});
