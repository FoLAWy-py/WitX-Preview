/** What a press, a drag or the wheel on the phone's picture in a takeover is sent as: a
 * click is a tap, a held click a long press, a drag a swipe along the way the pointer
 * took (see trail.ts), and the turns of the wheel one swipe. */

import { swipeOf, type TrailPoint } from "./trail";

/** A press that moves less than this (in page pixels) is a tap. */
export const TAP_SLOP = 8;
/** ... or, held at least this long (ms), a long press: what the phones take for one. */
export const LONG_PRESS = 500;
/** The longest long press the protocol takes (contracts: LongPress). */
export const LONG_PRESS_MAX_MS = 15000;

type Point = { x: number; y: number };
type Size = { width: number; height: number };

export type Gesture =
  | { kind: "tap"; x: number; y: number }
  | { kind: "long_press"; x: number; y: number; duration_ms: number }
  | ({ kind: "swipe" } & ReturnType<typeof swipeOf>);

/** The gesture of a press let go after `held` ms, `moved` page pixels from where it
 * began. `from` and `to` are where it began and ended, and `trail` the way it took
 * until it was let go, all in pixels of the picture of `size` it began on. */
export function gestureOf({
  from,
  to,
  moved,
  held,
  trail,
  size,
}: {
  from: Point;
  to: Point;
  moved: number;
  held: number;
  trail: TrailPoint[];
  size: Size;
}): Gesture {
  if (moved >= TAP_SLOP)
    return { kind: "swipe", ...swipeOf(trail, { ...to, t_ms: held }, size) };
  if (held >= LONG_PRESS)
    return {
      kind: "long_press",
      ...from,
      duration_ms: Math.min(LONG_PRESS_MAX_MS, held),
    };
  return { kind: "tap", ...from };
}

/** The swipe the wheel's turns of one gesture make, from the point under the pointer:
 * the way a finger would move (wheel down: the finger goes up), `total` page pixels of
 * the picture shown `shownHeight` pixels high, at most 40% of the screen, and always on
 * it. */
export function wheelSwipeOf(
  total: number,
  from: Point,
  size: Size,
  shownHeight: number,
): Extract<Gesture, { kind: "swipe" }> {
  const distance = Math.max(
    -size.height * 0.4,
    Math.min(size.height * 0.4, (total * size.height) / shownHeight),
  );
  const y2 = Math.round(
    Math.max(1, Math.min(size.height - 2, from.y - distance)),
  );
  return {
    kind: "swipe",
    x1: from.x,
    y1: from.y,
    x2: from.x,
    y2,
    duration_ms: 250,
  };
}

/** Whether two pictures show the screen the same way: the same size, turned the same
 * way (a phone turned right round keeps its size). Coordinates of one hold on the other. */
export function sameGeometry(
  a: Size & { rotation: number },
  b: Size & { rotation: number },
): boolean {
  return (
    a.width === b.width && a.height === b.height && a.rotation === b.rotation
  );
}
