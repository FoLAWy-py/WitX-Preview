import { useCallback, useState } from "react";

/** What this browser keeps for a person: a message being written, the settings
 * they last chose. Only a convenience: in a private window, or with storage blocked,
 * nothing is kept and the page works the same. */

function read(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? undefined : JSON.parse(raw);
  } catch {
    return undefined;
  }
}

function write(key: string, value: unknown): void {
  try {
    if (value === undefined || value === "") localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // kept only where storage works
  }
}

/** A string kept under `key` as it changes, and read back when the page comes again;
 * an empty one is not kept. With no key (no one signed in yet) it is only state. */
export function useStoredText(
  key: string | undefined,
): [string, (next: string | ((current: string) => string)) => void] {
  const load = (k: string | undefined) => {
    const value = k ? read(k) : undefined;
    return typeof value === "string" ? value : "";
  };
  const [state, setState] = useState(() => ({ key, text: load(key) }));
  // Another task's box in the same place: its own text.
  const current = state.key === key ? state : { key, text: load(key) };
  if (current !== state) setState(current);
  const set = useCallback(
    (next: string | ((current: string) => string)) =>
      setState((s) => {
        const text = typeof next === "function" ? next(s.text) : next;
        if (s.key) write(s.key, text);
        return { ...s, text };
      }),
    [],
  );
  return [current.text, set];
}

/** The settings a person last chose for a task, to start the next one with. */
export interface LastChoice {
  mode?: "ask" | "auto" | "full";
  model?: string;
  effort?: "low" | "medium" | "high" | "xhigh" | "max";
}

const lastKey = (userId: string) => `witx.last.${userId}`;

export function lastChoice(userId: string | undefined): LastChoice {
  const value = userId ? read(lastKey(userId)) : undefined;
  if (!value || typeof value !== "object") return {};
  const v = value as Record<string, unknown>;
  return {
    mode: ["ask", "auto", "full"].includes(v.mode as string)
      ? (v.mode as LastChoice["mode"])
      : undefined,
    model: typeof v.model === "string" ? v.model : undefined,
    effort: ["low", "medium", "high", "xhigh", "max"].includes(
      v.effort as string,
    )
      ? (v.effort as LastChoice["effort"])
      : undefined,
  };
}

export function rememberChoice(
  userId: string | undefined,
  patch: LastChoice,
): void {
  if (userId) write(lastKey(userId), { ...lastChoice(userId), ...patch });
}

/** Where a task's message box keeps what is being written. */
export const draftKey = (userId: string | undefined, place: string) =>
  userId ? `witx.draft.${userId}.${place}` : undefined;
