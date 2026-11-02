import { describe, expect, it } from "vitest";
import { matchesQuery } from "./search";

describe("matchesQuery", () => {
  it("finds what has every word of the query in any field, ignoring case", () => {
    const skill = ["pay-bill", "Pay a bill in an app", "WeChat"];
    expect(matchesQuery("pay wechat", skill)).toBe(true);
    expect(matchesQuery("BILL", skill)).toBe(true);
    expect(matchesQuery("pay alipay", skill)).toBe(false);
  });

  it("matches everything when the query is empty", () => {
    expect(matchesQuery("  ", ["anything"])).toBe(true);
  });
});
