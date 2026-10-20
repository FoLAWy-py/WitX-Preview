import { describe, expect, it } from "vitest";
import { parseSse } from "./sse";

describe("parseSse", () => {
  it("returns complete messages and keeps the unfinished rest", () => {
    const { messages, rest } = parseSse(
      'id: 1\nevent: a\ndata: {"x":1}\n\nid: 2\ndata: par',
    );
    expect(messages).toEqual([{ id: "1", event: "a", data: '{"x":1}' }]);
    expect(rest).toBe("id: 2\ndata: par");
  });

  it("ignores comments and keep-alives, joins multi-line data", () => {
    const { messages } = parseSse(": keep-alive\n\ndata: a\ndata: b\n\n");
    expect(messages).toEqual([{ data: "a\nb" }]);
  });

  it("accepts CRLF line endings", () => {
    expect(parseSse("data: x\r\n\r\n").messages).toEqual([{ data: "x" }]);
  });
});
