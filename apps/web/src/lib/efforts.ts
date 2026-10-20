import type { ModelView, TaskDetail } from "@/api/types.gen";

export type Effort = TaskDetail["reasoning_effort"];

/** A model's efforts when it is not in the list: medium and up, as every model offers. */
const FROM_MEDIUM: Effort[] = ["medium", "high", "xhigh", "max"];

/** The model a task runs on: the chosen one, else the default one allowed here, else the
 * first allowed, else the first there is. */
export function shownModel(
  models: ModelView[],
  chosen: string | undefined,
): string | undefined {
  const usable = models.filter((m) => m.allowed);
  return (
    chosen ??
    usable.find((m) => m.default)?.name ??
    usable[0]?.name ??
    models[0]?.name
  );
}

/** What a person may choose for a model, least first. */
export function effortsOf(
  models: ModelView[],
  name: string | undefined,
): Effort[] {
  return models.find((m) => m.name === name)?.efforts ?? FROM_MEDIUM;
}

/** The effort a task gets on a model: the chosen one, or that model's lowest when the
 * model does not go that low (the server raises it the same way on a switch). */
export function effortOn(efforts: Effort[], effort: Effort): Effort {
  return efforts.includes(effort) ? effort : efforts[0];
}
