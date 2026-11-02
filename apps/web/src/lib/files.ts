import type { FileView, TaskDetail, TaskInputView } from "@/api/types.gen";

/** A size as a phone shows it: 512 B, 2.4 KB, 13.0 MB (decimal units). */
export function byteSize(n: number): string {
  if (n < 1000) return `${n} B`;
  const kb = n / 1000;
  return kb < 1000 ? `${kb.toFixed(1)} KB` : `${(kb / 1000).toFixed(1)} MB`;
}

/**
 * The files that came with a message. Those sent with the first message have
 * no message number: they go with the task's first request, its first "goal" input.
 */
export function filesOf(task: TaskDetail, input: TaskInputView): FileView[] {
  const first = task.inputs.find((i) => i.kind === "goal")?.seq;
  return (task.files ?? []).filter((f) =>
    f.input_seq == null ? input.seq === first : f.input_seq === input.seq,
  );
}

/** The most files one message carries, and the largest file (the server's limits,
 * `rules.files`): said before a person picks, and checked before anything is sent. */
export const MAX_FILES = 10;
export const MAX_FILE_BYTES = 100 * 1024 * 1024;
export const MAX_FILE_SIZE = `${MAX_FILE_BYTES / 1024 / 1024} MB`;

/**
 * Which of the files just picked are added, when `already` are picked for the message:
 * one over the size limit is left out (`tooLarge`), and none past the per-message count
 * (`over`, how many). Nothing left out is uploaded.
 */
export function fitFiles<T extends { size: number }>(
  files: T[],
  already: number,
): { take: T[]; tooLarge: T[]; over: number } {
  const tooLarge = files.filter((f) => f.size > MAX_FILE_BYTES);
  const fitting = files.filter((f) => f.size <= MAX_FILE_BYTES);
  const room = Math.max(0, MAX_FILES - already);
  return {
    take: fitting.slice(0, room),
    tooLarge,
    over: Math.max(0, fitting.length - room),
  };
}
