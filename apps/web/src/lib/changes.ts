import type {
  EventView,
  NoteView,
  TaskChanges,
  TaskDetail,
  TaskEarlier,
} from "@/api/types.gen";

/** The most unfinished records a page names when it asks for changes (the API's
 * OPEN_HELD_MAX); holding more, it fetches the whole task. */
export const OPEN_HELD_MAX = 150;

const OPEN_ACTION = new Set(["awaiting_approval", "queued", "sent"]);
const OPEN_SHOT = new Set(["requested", "sent"]);
const OVER_RUN = new Set(["completed", "failed", "cancelled"]);

/** The actions, screenshots and runs the page holds as not finished: it names them when
 * it asks for changes, since some finish with no event that names them. */
export function heldOpen(task: TaskDetail): string[] {
  return [
    ...task.actions.filter((a) => OPEN_ACTION.has(a.status)).map((a) => a.id),
    ...task.observations
      .filter((o) => OPEN_SHOT.has(o.status))
      .map((o) => o.id),
    ...task.runs.filter((r) => !OVER_RUN.has(r.status)).map((r) => r.id),
  ];
}

/** Records as they were, each one sent again replaced where it stands, new ones after. */
function upsert<T extends { id: string }>(was: T[], sent: T[]): T[] {
  const byId = new Map(was.map((r) => [r.id, r]));
  for (const record of sent) byId.set(record.id, record);
  return [...byId.values()];
}

/** The notes as they were, each sent run's notes put in place of its old ones (where its
 * first note stood), the notes of a run new to the page after. */
function replaceNotes(
  was: NoteView[],
  sent: NoteView[],
  runs: Set<string>,
): NoteView[] {
  const fresh = new Map<string, NoteView[]>();
  for (const note of sent)
    fresh.set(note.run_id, [...(fresh.get(note.run_id) ?? []), note]);
  const out: NoteView[] = [];
  const placed = new Set<string>();
  for (const note of was) {
    if (!runs.has(note.run_id)) out.push(note);
    else if (!placed.has(note.run_id)) {
      placed.add(note.run_id);
      out.push(...(fresh.get(note.run_id) ?? []));
    }
  }
  for (const [run, notes] of fresh) if (!placed.has(run)) out.push(...notes);
  return out;
}

/** The task once `changes` (fetched after the task's `last_event_seq`) are applied: its
 * own fields and short lists as sent, the actions, screenshots and runs sent replaced or
 * added, and the sent runs' notes in place of theirs. */
export function applyChanges(
  task: TaskDetail,
  changes: TaskChanges,
): TaskDetail {
  const { since: _since, ...sent } = changes;
  return {
    ...sent,
    // The part the page holds is its own: changes do not say.
    earlier: task.earlier,
    events: [],
    actions: upsert(task.actions, sent.actions),
    observations: upsert(task.observations, sent.observations),
    runs: upsert(task.runs, sent.runs),
    notes: replaceNotes(
      task.notes,
      sent.notes,
      new Set(sent.runs.map((r) => r.id)),
    ),
  };
}

/** Records of an earlier part before those the page holds, each once. */
function before<T extends { id: string }>(sent: T[], held: T[]): T[] {
  const ids = new Set(held.map((r) => r.id));
  return [...sent.filter((r) => !ids.has(r.id)), ...held];
}

/** The task with the part before what it holds put in front of it. */
export function applyEarlier(task: TaskDetail, part: TaskEarlier): TaskDetail {
  return {
    ...task,
    actions: before(part.actions, task.actions),
    observations: before(part.observations, task.observations),
    notes: [...part.notes, ...task.notes],
    earlier: part.earlier,
  };
}

/** Events in order, each once: those held and those `more` brings (later, earlier or
 * the same ones again). */
export function mergeEvents(held: EventView[], more: EventView[]): EventView[] {
  if (!more.length) return held;
  const last = held.length ? held[held.length - 1].seq : 0;
  if (more.every((e, i) => e.seq > (i ? more[i - 1].seq : last)))
    return [...held, ...more];
  const bySeq = new Map(held.map((e) => [e.seq, e]));
  for (const event of more) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}
