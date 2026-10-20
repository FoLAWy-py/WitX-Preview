import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamHandlers } from "@/api/client";
import type { EventView } from "@/api/types.gen";
import { follow, SILENT_MS, type OpenStream } from "./follow";

const event = (seq: number) =>
  ({ seq, type: "x", data: {} }) as unknown as EventView;

/** A stream that stays open until aborted. */
function openUntilAborted(handlers: StreamHandlers, signal: AbortSignal) {
  handlers.onOpen();
  return new Promise<void>((_, reject) =>
    signal.addEventListener("abort", () => reject(new Error("aborted"))),
  );
}

const never = () => () => {};

describe("follow", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("resumes after the last event and reports the reconnect", async () => {
    const afters: number[] = [];
    const connections: [boolean, boolean][] = [];
    const open: OpenStream = async (after, handlers, signal) => {
      afters.push(after);
      if (afters.length === 1) {
        handlers.onOpen();
        handlers.onEvent(event(3));
        return; // the stream ends
      }
      return openUntilAborted(handlers, signal);
    };
    const stop = follow(
      open,
      {
        onEvent: () => {},
        onConnection: (up, again) => connections.push([up, again]),
      },
      never,
    );
    await vi.advanceTimersByTimeAsync(1_000);
    stop();
    expect(afters).toEqual([0, 3]);
    expect(connections).toEqual([
      [true, false],
      [false, false],
      [true, true],
    ]);
  });

  it("backs off after failed attempts and starts over once connected", async () => {
    const starts: number[] = [];
    const open: OpenStream = async (_after, handlers, signal) => {
      starts.push(Date.now());
      if (starts.length <= 3) throw new TypeError("Failed to fetch");
      return openUntilAborted(handlers, signal);
    };
    const t0 = Date.now();
    const stop = follow(
      open,
      { onEvent: () => {}, onConnection: () => {} },
      never,
    );
    await vi.advanceTimersByTimeAsync(10_000);
    stop();
    expect(starts.map((t) => t - t0)).toEqual([0, 1_000, 3_000, 7_000]);
  });

  it("replaces a connection that has gone silent", async () => {
    let opens = 0;
    const open: OpenStream = async (_after, handlers, signal) => {
      opens++;
      return openUntilAborted(handlers, signal);
    };
    const stop = follow(
      open,
      { onEvent: () => {}, onConnection: () => {} },
      never,
    );
    await vi.advanceTimersByTimeAsync(SILENT_MS - 1);
    expect(opens).toBe(1);
    await vi.advanceTimersByTimeAsync(1_000);
    stop();
    expect(opens).toBe(2);
  });

  it("reconnects at once when the user comes back", async () => {
    let opens = 0;
    let back = () => {};
    const open: OpenStream = async () => {
      opens++;
      throw new TypeError("Failed to fetch");
    };
    const stop = follow(
      open,
      { onEvent: () => {}, onConnection: () => {} },
      (cb) => {
        back = cb;
        return () => {};
      },
    );
    await vi.advanceTimersByTimeAsync(100); // first attempt failed; waiting 1 s
    expect(opens).toBe(1);
    back();
    await vi.advanceTimersByTimeAsync(0);
    stop();
    expect(opens).toBe(2);
  });
});
