import { describe, expect, it } from "vitest";
import { advance, CATCH_UP_MS } from "@/lib/typewriter";

describe("advance", () => {
  it("moves toward the target at a pace that catches up within the catch-up time", () => {
    const target = "x".repeat(100);
    expect(advance("", target, CATCH_UP_MS / 2).length).toBe(50);
    expect(advance("x".repeat(50), target, CATCH_UP_MS / 2).length).toBe(75);
    expect(advance("x".repeat(99), target, 1)).toBe(target); // never less than a character
    expect(advance(target, target, 100)).toBe(target);
  });

  it("replaces at once what the agent started over", () => {
    expect(advance("It is 8:07.", "Settings is open.", 16)).toBe(
      "Settings is open.",
    );
  });
});
