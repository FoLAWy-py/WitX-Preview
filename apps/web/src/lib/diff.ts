/** A line of a comparison between two texts. */
export interface DiffLine {
  kind: "same" | "added" | "removed";
  text: string;
}

/**
 * What changed between two texts, line by line: the longest run of lines they share
 * stays, the rest is removed from the first or added in the second. Shown before a
 * skill is saved, so the person sees what the agent will follow.
 */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before ? before.split("\n") : [];
  const b = after ? after.split("\n") : [];
  // shared[i][j]: the most lines a[i:] and b[j:] have in common, in order.
  const shared = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      shared[i][j] =
        a[i] === b[j]
          ? shared[i + 1][j + 1] + 1
          : Math.max(shared[i + 1][j], shared[i][j + 1]);
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      lines.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (
      i < a.length &&
      (j === b.length || shared[i + 1][j] >= shared[i][j + 1])
    ) {
      lines.push({ kind: "removed", text: a[i++] });
    } else {
      lines.push({ kind: "added", text: b[j++] });
    }
  }
  return lines;
}
