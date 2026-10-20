import { describe, expect, it } from "vitest";
import { lineDiff } from "./diff";

describe("lineDiff", () => {
  it("keeps the lines both texts share and marks the rest", () => {
    expect(lineDiff("open\ntap Save\ndone", "open\ntap Done\ndone")).toEqual([
      { kind: "same", text: "open" },
      { kind: "removed", text: "tap Save" },
      { kind: "added", text: "tap Done" },
      { kind: "same", text: "done" },
    ]);
  });

  it("shows a new text as all added and an emptied one as all removed", () => {
    expect(lineDiff("", "a\nb").map((l) => l.kind)).toEqual(["added", "added"]);
    expect(lineDiff("a", "").map((l) => l.kind)).toEqual(["removed"]);
  });
});
