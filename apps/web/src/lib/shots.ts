import { ApiError } from "@/api/client";

/** What a view of a screenshot shows: the picture once loaded, or why it is not there. */
export interface Shot {
  url?: string;
  failed: boolean;
  /** Deleted under the retention policy. */
  purged: boolean;
}

type Fetch = (signal: AbortSignal) => Promise<Blob>;

interface Entry {
  fetch: Fetch;
  shot: Shot;
  bytes: number;
  /** Views showing it now; at none, a loaded picture waits in memory to be let go. */
  users: number;
  listeners: Set<(shot: Shot) => void>;
  controller?: AbortController;
  /** When the last view let go of it, for letting the oldest go first. */
  idleSince: number;
}

/** Screenshots loading at once, so the task's other requests (the event stream, an
 * approval) still find a free connection on HTTP/1.1, which allows six per site. */
const LOADING_MAX = 3;
/** Memory for pictures no view shows now, kept so scrolling back shows them at once. */
const IDLE_BYTES = 48 * 1024 * 1024;

/**
 * The screenshots of this page, by observation and width: one request for a picture
 * however many views show it, a few requests at a time, and an object URL that stays as
 * long as a view shows it. A picture asked for and let go before its turn is never
 * fetched; one let go while loading is cancelled.
 */
export class Shots {
  private entries = new Map<string, Entry>();
  private waiting: string[] = [];
  private loading = 0;
  private clock = 0;

  constructor(
    private readonly limits = { loading: LOADING_MAX, idleBytes: IDLE_BYTES },
  ) {}

  /** Shows the picture at `key` to `listener` (at once with what is known) until the
   * returned function lets it go. */
  watch(key: string, fetch: Fetch, listener: (shot: Shot) => void): () => void {
    let entry = this.entries.get(key);
    if (!entry) {
      entry = {
        fetch,
        shot: { failed: false, purged: false },
        bytes: 0,
        users: 0,
        listeners: new Set(),
        idleSince: 0,
      };
      this.entries.set(key, entry);
      this.waiting.push(key);
    }
    entry.users += 1;
    entry.listeners.add(listener);
    listener(entry.shot);
    this.pump();
    let watching = true;
    return () => {
      if (!watching) return;
      watching = false;
      this.letGo(key, listener);
    };
  }

  private letGo(key: string, listener: (shot: Shot) => void) {
    const entry = this.entries.get(key);
    if (!entry) return;
    entry.listeners.delete(listener);
    entry.users -= 1;
    if (entry.users > 0) return;
    if (entry.shot.url) {
      entry.idleSince = ++this.clock;
      this.trim();
      return;
    }
    // Not loaded: drop it, so a later view asks again (a failure may have passed).
    this.waiting = this.waiting.filter((k) => k !== key);
    entry.controller?.abort();
    this.entries.delete(key);
  }

  private pump() {
    while (this.loading < this.limits.loading && this.waiting.length) {
      const key = this.waiting.shift()!;
      const entry = this.entries.get(key);
      if (entry) this.load(key, entry);
    }
  }

  private load(key: string, entry: Entry) {
    const controller = new AbortController();
    entry.controller = controller;
    this.loading += 1;
    entry
      .fetch(controller.signal)
      .then((blob) => {
        if (this.entries.get(key) !== entry) return;
        entry.bytes = blob.size;
        this.settle(entry, {
          url: URL.createObjectURL(blob),
          failed: false,
          purged: false,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || this.entries.get(key) !== entry)
          return;
        const purged = error instanceof ApiError && error.code === "purged";
        this.settle(entry, { failed: true, purged });
      })
      .finally(() => {
        entry.controller = undefined;
        this.loading -= 1;
        this.pump();
      });
  }

  private settle(entry: Entry, shot: Shot) {
    entry.shot = shot;
    for (const listener of entry.listeners) listener(shot);
  }

  /** Lets the pictures no view shows go, oldest first, until they fit in memory. */
  private trim() {
    const idle = [...this.entries]
      .filter(([, e]) => e.users === 0 && e.shot.url)
      .sort(([, a], [, b]) => a.idleSince - b.idleSince);
    let bytes = idle.reduce((sum, [, e]) => sum + e.bytes, 0);
    for (const [key, entry] of idle) {
      if (bytes <= this.limits.idleBytes) break;
      URL.revokeObjectURL(entry.shot.url!);
      this.entries.delete(key);
      bytes -= entry.bytes;
    }
  }
}

/** The page's one set of screenshots. */
export const shots = new Shots();
