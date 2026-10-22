import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/client";
import { Shots, type Shot } from "./shots";

/** A fetch that answers when told to, and records what it was asked. */
function server() {
  const asked: string[] = [];
  const pending = new Map<
    string,
    { resolve: (b: Blob) => void; reject: (e: unknown) => void }
  >();
  const aborted: string[] = [];
  const fetchOf = (key: string) => (signal: AbortSignal) => {
    asked.push(key);
    signal.addEventListener("abort", () => aborted.push(key));
    return new Promise<Blob>((resolve, reject) =>
      pending.set(key, { resolve, reject }),
    );
  };
  const answer = async (key: string, bytes = 10) => {
    pending.get(key)!.resolve(new Blob([new Uint8Array(bytes)]));
    await settle();
  };
  const fail = async (key: string, error: unknown) => {
    pending.get(key)!.reject(error);
    await settle();
  };
  return { asked, aborted, fetchOf, answer, fail };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function view(
  shots: Shots,
  key: string,
  fetch: ReturnType<ReturnType<typeof server>["fetchOf"]>,
) {
  const seen: Shot[] = [];
  const stop = shots.watch(key, fetch, (shot) => seen.push(shot));
  return { seen, stop, last: () => seen[seen.length - 1] };
}

describe("screenshots", () => {
  it("fetches a picture once however many views show it", async () => {
    const shots = new Shots();
    const s = server();
    const a = view(shots, "o1", s.fetchOf("o1"));
    const b = view(shots, "o1", s.fetchOf("o1"));
    await s.answer("o1");

    expect(s.asked).toEqual(["o1"]);
    expect(a.last().url).toBeDefined();
    expect(b.last().url).toBe(a.last().url);
  });

  it("loads a few at a time, and never fetches one let go before its turn", async () => {
    const shots = new Shots({ loading: 2, idleBytes: 1000 });
    const s = server();
    view(shots, "o1", s.fetchOf("o1"));
    view(shots, "o2", s.fetchOf("o2"));
    const third = view(shots, "o3", s.fetchOf("o3"));
    view(shots, "o4", s.fetchOf("o4"));
    expect(s.asked).toEqual(["o1", "o2"]);

    third.stop();
    await s.answer("o1");

    expect(s.asked).toEqual(["o1", "o2", "o4"]);
  });

  it("cancels a picture let go while it loads", async () => {
    const shots = new Shots();
    const s = server();
    view(shots, "o1", s.fetchOf("o1")).stop();

    expect(s.aborted).toEqual(["o1"]);
  });

  it("keeps pictures no view shows until they pass the memory allowed, letting the oldest go first", async () => {
    const shots = new Shots({ loading: 3, idleBytes: 25 });
    const s = server();
    const views = ["o1", "o2", "o3"].map((key) =>
      view(shots, key, s.fetchOf(key)),
    );
    for (const key of ["o1", "o2", "o3"]) await s.answer(key, 10);
    for (const v of views) v.stop();

    // 30 bytes idle, 25 allowed: o1, let go first, is gone; o2 and o3 come back at once.
    view(shots, "o2", s.fetchOf("o2"));
    view(shots, "o3", s.fetchOf("o3"));
    view(shots, "o1", s.fetchOf("o1"));
    expect(s.asked).toEqual(["o1", "o2", "o3", "o1"]);
  });

  it("says a deleted picture was purged, and asks again for one that failed once no view shows it", async () => {
    const shots = new Shots();
    const s = server();
    const gone = view(shots, "o1", s.fetchOf("o1"));
    await s.fail("o1", new ApiError(410, "purged", "deleted"));
    expect(gone.last()).toEqual({ failed: true, purged: true });

    gone.stop();
    view(shots, "o1", s.fetchOf("o1"));
    expect(s.asked).toEqual(["o1", "o1"]);
  });
});
