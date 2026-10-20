import { describe, expect, it } from "vitest";
import { base64Bytes, liveDelay } from "./frame";

describe("base64Bytes", () => {
  it("reads the bytes of a picture sent as base64", () => {
    expect([...base64Bytes("/9j/4AAQ")]).toEqual([255, 216, 255, 224, 0, 16]);
  });

  it("reads an empty string as no bytes", () => {
    expect(base64Bytes("").length).toBe(0);
  });
});

describe("liveDelay", () => {
  it("asks every second, then less and less often while no picture comes, up to 10 s", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 20].map(liveDelay)).toEqual([
      1_000, 1_000, 1_000, 2_000, 4_000, 8_000, 10_000, 10_000,
    ]);
  });
});
