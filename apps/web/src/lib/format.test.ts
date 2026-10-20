import { describe, expect, it } from "vitest";
import { duration, moment } from "./format";

describe("moment", () => {
  const now = new Date(2026, 9, 16, 18, 0);
  it("says only the time for today, adds the date before, and the year before this one", () => {
    expect(moment(new Date(2026, 9, 16, 13, 16).toISOString(), "en", now)).toBe(
      "13:16",
    );
    expect(moment(new Date(2026, 9, 14, 9, 5).toISOString(), "en", now)).toBe(
      "Oct 14, 09:05",
    );
    expect(
      moment(new Date(2025, 11, 31, 23, 59).toISOString(), "en", now),
    ).toBe("Dec 31, 2025, 23:59");
  });
});

describe("duration", () => {
  const t = (key: string, vars: Record<string, unknown> = {}) =>
    ({
      "time.seconds": `${vars.n}s`,
      "time.minutes": `${vars.m}m ${vars.s}s`,
      "time.hours": `${vars.h}h ${vars.m}m ${vars.s}s`,
    })[key] ?? key;
  it("counts seconds, then minutes and seconds, then hours, minutes and seconds", () => {
    expect(duration(45_900, t as never)).toBe("45s");
    expect(duration(65_000, t as never)).toBe("1m 05s");
    expect(duration(3_725_000, t as never)).toBe("1h 02m 05s");
  });
});
