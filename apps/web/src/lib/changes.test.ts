import { describe, expect, it } from "vitest";
import type {
  EventView,
  TaskChanges,
  TaskDetail,
  TaskEarlier,
} from "@/api/types.gen";
import { applyChanges, applyEarlier, heldOpen, mergeEvents } from "./changes";

const action = (id: string, status: string) => ({ id, status });
const run = (id: string, status: string) => ({ id, status });
const note = (run_id: string, text: string) => ({
  run_id,
  text,
  observation_id: null,
  seen_at: null,
});

function task(parts: object): TaskDetail {
  return {
    status: "active",
    actions: [],
    observations: [],
    runs: [],
    notes: [],
    inputs: [],
    last_event_seq: 1,
    ...parts,
  } as unknown as TaskDetail;
}

function changes(parts: object): TaskChanges {
  return { ...task(parts), since: 1 } as unknown as TaskChanges;
}

describe("a task's changes", () => {
  it("replace the records sent where they stand and add new ones after", () => {
    const before = task({
      actions: [
        action("a1", "done"),
        action("a2", "sent"),
        action("a3", "done"),
      ],
    });
    const after = applyChanges(
      before,
      changes({ actions: [action("a2", "done"), action("a4", "queued")] }),
    );

    expect(after.actions).toEqual([
      action("a1", "done"),
      action("a2", "done"),
      action("a3", "done"),
      action("a4", "queued"),
    ]);
  });

  it("take the task's own fields and short lists as sent", () => {
    const before = task({
      status: "active",
      inputs: [{ seq: 1 }],
      last_event_seq: 4,
    });
    const after = applyChanges(
      before,
      changes({
        status: "completed",
        inputs: [{ seq: 1 }, { seq: 2 }],
        last_event_seq: 9,
      }),
    );

    expect(after.status).toBe("completed");
    expect(after.inputs).toEqual([{ seq: 1 }, { seq: 2 }]);
    expect(after.last_event_seq).toBe(9);
    expect("since" in after).toBe(false);
  });

  it("put a sent run's notes in place of its old ones, and leave the other runs' notes", () => {
    const before = task({
      runs: [run("r1", "completed"), run("r2", "running")],
      notes: [note("r1", "one"), note("r2", "two"), note("r1", "three")],
    });
    const after = applyChanges(
      before,
      changes({
        runs: [run("r1", "completed"), run("r3", "running")],
        notes: [
          note("r1", "one"),
          note("r1", "three"),
          note("r1", "four"),
          note("r3", "new"),
        ],
      }),
    );

    expect(after.notes.map((n) => n.text)).toEqual([
      "one",
      "three",
      "four",
      "two",
      "new",
    ]);
    expect(after.runs.map((r) => r.id)).toEqual(["r1", "r2", "r3"]);
  });

  it("are asked for naming the actions, screenshots and runs not finished", () => {
    const held = task({
      actions: [action("a1", "done"), action("a2", "awaiting_approval")],
      observations: [
        { id: "o1", status: "complete" },
        { id: "o2", status: "sent" },
      ],
      runs: [run("r1", "completed"), run("r2", "waiting")],
    });

    expect(heldOpen(held)).toEqual(["a2", "o2", "r2"]);
  });

  it("keep the part the page holds", () => {
    const held = task({ earlier: { before: "2026-10-10T10:00:00Z" } });
    expect(applyChanges(held, changes({ earlier: null })).earlier).toEqual({
      before: "2026-10-10T10:00:00Z",
    });
  });
});

describe("an earlier part of a task", () => {
  it("goes in front of what the page holds, each record once", () => {
    const held = task({
      actions: [action("a2", "done"), action("a3", "awaiting_approval")],
      notes: [note("r2", "two")],
      earlier: { before: "2026-10-10T10:00:00Z" },
    });
    const after = applyEarlier(held, {
      actions: [action("a1", "done"), action("a3", "awaiting_approval")],
      observations: [],
      notes: [note("r1", "one")],
      events: [],
      earlier: null,
    } as unknown as TaskEarlier);

    expect(after.actions.map((a) => a.id)).toEqual(["a1", "a2", "a3"]);
    expect(after.notes.map((n) => n.text)).toEqual(["one", "two"]);
    expect(after.earlier).toBeNull();
  });
});

describe("a conversation's events", () => {
  const event = (seq: number) => ({ seq }) as EventView;

  it("stay in order and each once, whether more come after, before or again", () => {
    const held = [event(5), event(7)];
    expect(mergeEvents(held, [event(8)]).map((e) => e.seq)).toEqual([5, 7, 8]);
    expect(
      mergeEvents(held, [event(2), event(5), event(9)]).map((e) => e.seq),
    ).toEqual([2, 5, 7, 9]);
    expect(mergeEvents(held, [])).toBe(held);
  });
});
