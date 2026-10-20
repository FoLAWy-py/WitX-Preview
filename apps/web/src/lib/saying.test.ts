import { describe, expect, it } from "vitest";
import type { RunView } from "@/api/types.gen";
import { sayingOf } from "@/lib/saying";
import type { TimelineItem } from "@/lib/timeline";

const run = { id: "r1" } as RunView;
const message = (text: string) =>
  ({ kind: "message", at: "", key: "m", text, runId: "r1" }) as TimelineItem;

describe("sayingOf", () => {
  it("shows the agent's words while they come", () => {
    expect(sayingOf({ run_id: "r1", text: "It is 8:07. " }, run, [])).toBe(
      "It is 8:07.",
    );
  });

  it("shows nothing for another run, for no run, for no words, or once the words are kept", () => {
    expect(sayingOf({ run_id: "r2", text: "x" }, run, [])).toBeNull();
    expect(sayingOf({ run_id: "r1", text: "x" }, undefined, [])).toBeNull();
    expect(sayingOf({ run_id: "r1", text: "  " }, run, [])).toBeNull();
    expect(
      sayingOf({ run_id: "r1", text: "x" }, run, [message("x")]),
    ).toBeNull();
    expect(sayingOf(null, run, [])).toBeNull();
  });
});
