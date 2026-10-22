import type { StreamHandlers } from "@/api/client";
import type { EventView, SayingView } from "@/api/types.gen";

/** The server sends a keep-alive every 15 s; silence this long means a dead connection. */
export const SILENT_MS = 45_000;
/** On returning to the page, a connection silent this long is replaced at once. */
export const STALE_ON_RETURN_MS = 20_000;
/** Waits before reconnecting after failed attempts; it resets once connected. */
export const BACKOFF_MS = [1_000, 2_000, 4_000, 10_000];
/** Wait before reconnecting after a stream that was open ended. */
export const AFTER_END_MS = 500;

export type OpenStream = (
  after: number,
  handlers: StreamHandlers,
  signal: AbortSignal,
) => Promise<void>;

export interface FollowHandlers {
  onEvent: (event: EventView) => void;
  onSaying?: (saying: SayingView) => void;
  /** `reconnected`: an earlier connection existed, so changes may have been missed. */
  onConnection: (connected: boolean, reconnected: boolean) => void;
}

/** Registers `callback` for "the user is back": the network returned or the page is visible. */
export type OnReturn = (callback: () => void) => () => void;

export const onReturn: OnReturn = (callback) => {
  const visible = () => {
    if (document.visibilityState === "visible") callback();
  };
  window.addEventListener("online", callback);
  document.addEventListener("visibilitychange", visible);
  return () => {
    window.removeEventListener("online", callback);
    document.removeEventListener("visibilitychange", visible);
  };
};

/**
 * Follows an event stream: resumes after the last event seen, reconnects with
 * backoff, replaces a connection that has gone silent, and reconnects at once when
 * the user returns. Returns a function that stops following.
 */
export function follow(
  open: OpenStream,
  handlers: FollowHandlers,
  whenBack: OnReturn = onReturn,
  /** The last event already had: the stream starts after it. */
  start = 0,
): () => void {
  let stopped = false;
  let cursor = start;
  let failures = 0;
  let everOpen = false;
  let lastActivity = 0;
  let current: AbortController | undefined;
  let endSleep: (() => void) | undefined;

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const timer = setTimeout(done, ms);
      function done() {
        clearTimeout(timer);
        endSleep = undefined;
        resolve();
      }
      endSleep = done;
    });

  const stopWatching = whenBack(() => {
    if (endSleep) endSleep();
    else if (Date.now() - lastActivity > STALE_ON_RETURN_MS) current?.abort();
  });

  const run = async () => {
    while (!stopped) {
      const controller = new AbortController();
      current = controller;
      let watchdog: ReturnType<typeof setTimeout> | undefined;
      const alive = () => {
        lastActivity = Date.now();
        clearTimeout(watchdog);
        watchdog = setTimeout(() => controller.abort(), SILENT_MS);
      };
      let opened = false;
      try {
        await open(
          cursor,
          {
            onOpen: () => {
              opened = true;
              failures = 0;
              alive();
              handlers.onConnection(true, everOpen);
              everOpen = true;
            },
            onEvent: (event) => {
              cursor = event.seq;
              alive();
              handlers.onEvent(event);
            },
            onSaying: (saying) => {
              alive();
              handlers.onSaying?.(saying);
            },
            onActivity: alive,
          },
          controller.signal,
        );
      } catch {
        // dropped, refused or aborted: reconnect below
      }
      clearTimeout(watchdog);
      if (stopped) return;
      handlers.onConnection(false, false);
      await sleep(
        opened
          ? AFTER_END_MS
          : BACKOFF_MS[Math.min(failures++, BACKOFF_MS.length - 1)],
      );
    }
  };
  void run();

  return () => {
    stopped = true;
    current?.abort();
    endSleep?.();
    stopWatching();
  };
}
