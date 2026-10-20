import type { RunView, SayingView } from "@/api/types.gen";
import type { TimelineItem } from "@/lib/timeline";

/** The agent's words as they come, to show as a message in the making: nothing
 * when they are another run's, or once the conversation already holds a message or a
 * result with the same words (the step is over and what it said is on record). */
export function sayingOf(
  saying: SayingView | null | undefined,
  run: RunView | undefined,
  items: TimelineItem[],
): string | null {
  if (!saying || !run || saying.run_id !== run.id) return null;
  const text = saying.text.trim();
  if (!text) return null;
  const kept = items.some(
    (i) =>
      (i.kind === "message" && i.text.trim() === text) ||
      (i.kind === "result" && i.result.summary.trim() === text),
  );
  return kept ? null : text;
}
