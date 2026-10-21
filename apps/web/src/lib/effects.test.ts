import { describe, expect, it } from "vitest";
import { effectObject, kindKey } from "./effects";

describe("effectObject", () => {
  it("names what the agent said the input changes", () => {
    const planted = { changes: "plant cherries", intent: null };
    expect(effectObject("change_data", planted, "en")).toBe("plant cherries");
  });

  it("names the amount of a payment and the person a message goes to", () => {
    const paid = {
      changes: "plant cherries",
      intent: { payee: "Farm", amount: "874", currency: "游戏币" },
    };
    expect(effectObject("payment", paid, "zh-CN")).toBe("874 游戏币");
    expect(effectObject("change_data", paid, "zh-CN")).toBe("plant cherries");
    const sent = { changes: null, intent: { recipient: "Alice", body: "Hi" } };
    expect(effectObject("send_message", sent, "en")).toBe("Alice");
  });

  it("is nothing when neither the terms nor the agent say", () => {
    expect(effectObject("change_data", { intent: null }, "en")).toBeNull();
    expect(
      effectObject("payment", { changes: "", intent: {} }, "en"),
    ).toBeNull();
  });
});

describe("kindKey", () => {
  it("calls typing that empties the field first a replacement", () => {
    const typed = { kind: "type_text" as const, input: { text: "Ann" } };
    expect(kindKey(typed)).toBe("kind.type_text");
    expect(kindKey({ ...typed, input: { text: "Ann", replace: true } })).toBe(
      "kind.type_text_replace",
    );
    expect(kindKey({ kind: "tap", input: { x: 1, y: 2 } })).toBe("kind.tap");
  });

  it("says when text was tapped in on the keyboard or pasted", () => {
    const typed = { kind: "type_text" as const, input: { text: "你好" } };
    expect(kindKey({ ...typed, input: { text: "你好", method: "keys" } })).toBe(
      "kind.type_text_keys",
    );
    expect(
      kindKey({
        ...typed,
        input: { text: "你好", method: "paste", replace: true },
      }),
    ).toBe("kind.type_text_paste");
    expect(kindKey({ ...typed, input: { text: "你好", method: "set" } })).toBe(
      "kind.type_text",
    );
  });
});
