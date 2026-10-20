/** How the agent's words appear: not in the bursts they arrive in, but at an even
 * pace that catches up with what has arrived within about CATCH_UP_MS (the way Codex
 * commits streamed text at a steady cadence). A target that is not a continuation of
 * what is shown (the agent started over) replaces it at once. */

export const CATCH_UP_MS = 350;

/** The text to show after `dtMs`, on the way from `shown` to `target`. */
export function advance(shown: string, target: string, dtMs: number): string {
  if (!target.startsWith(shown)) return target;
  const behind = target.length - shown.length;
  if (behind <= 0) return shown;
  const step = Math.max(1, Math.ceil((behind * dtMs) / CATCH_UP_MS));
  return target.slice(0, shown.length + Math.min(step, behind));
}
