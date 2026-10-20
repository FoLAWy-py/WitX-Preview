import { describe, expect, it } from "vitest";
import type { ModelView } from "@/api/types.gen";
import { effortOn, effortsOf } from "./efforts";

const models: ModelView[] = [
  {
    name: "gpt-6-luna",
    provider: "openai",
    default: true,
    allowed: true,
    efforts: ["medium", "high", "xhigh", "max"],
  },
  {
    name: "gpt-6.1-sol",
    provider: "openai",
    default: false,
    allowed: true,
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
];

describe("efforts", () => {
  it("offers each model the efforts it goes down to", () => {
    expect(effortsOf(models, "gpt-6.1-sol")[0]).toBe("low");
    expect(effortsOf(models, "gpt-6-luna")[0]).toBe("medium");
    expect(effortsOf(models, "gone")[0]).toBe("medium");
  });

  it("raises an effort a model does not go down to, and keeps any other", () => {
    const luna = effortsOf(models, "gpt-6-luna");
    expect(effortOn(luna, "low")).toBe("medium");
    expect(effortOn(luna, "xhigh")).toBe("xhigh");
    expect(effortOn(effortsOf(models, "gpt-6.1-sol"), "low")).toBe("low");
  });
});
