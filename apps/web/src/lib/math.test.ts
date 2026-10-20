import { describe, expect, it } from "vitest";
import { normalizeMath } from "./math";

describe("math in agent text", () => {
  it("makes a line that is only $$…$$ a display block", () => {
    expect(normalizeMath("$$x^2$$")).toBe("$$\nx^2\n$$");
  });

  it("reads \\[…\\] as a display block and \\(…\\) as inline math", () => {
    expect(normalizeMath("a \\(x\\) b")).toBe("a $x$ b");
    expect(normalizeMath("\\[ \\frac{1}{2} \\]")).toBe(
      "\n$$\n\\frac{1}{2}\n$$\n",
    );
  });

  it("leaves code alone", () => {
    const code = "```latex\n\\[ x \\]\n```";
    expect(normalizeMath(code)).toBe(code);
    expect(normalizeMath("use `\\(x\\)` here")).toBe("use `\\(x\\)` here");
  });
});
