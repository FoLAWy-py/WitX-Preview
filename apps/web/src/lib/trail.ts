/** The way a pointer takes across the phone's picture during a drag in a takeover:
 * sampled while it is pressed, sent with the swipe as its path, so the phone replays the
 * drag as it was made (a slider puzzle is dragged, not jumped to), and drawn on the picture
 * meanwhile, so the person sees where they have dragged to without waiting for the phone. */

/** A point of the picture, in its pixels, reached this long (ms) after the press. */
export type TrailPoint = { x: number; y: number; t_ms: number };

/** A point is kept when this long has passed since the last one kept: about one a frame,
 * which is all a replay can use. */
export const SAMPLE_MS = 16;
/** The protocol takes at most this many points in a path (contracts: PATH_MAX). */
export const PATH_MAX = 200;
/** A swipe's duration_ms runs from this to this (contracts: Swipe). A drag shorter than the
 * least is a flick with hardly any samples; one longer than the most is sent as a straight
 * swipe of the longest duration, as before there were paths. */
export const DURATION_MIN_MS = 50;
export const DURATION_MAX_MS = 5000;

/** Adds `point` to `trail` when SAMPLE_MS has passed since the last point kept (the first
 * point is always kept); says whether it was added. */
export function sample(trail: TrailPoint[], point: TrailPoint): boolean {
  const last = trail[trail.length - 1];
  if (last && point.t_ms - last.t_ms < SAMPLE_MS) return false;
  trail.push(point);
  return true;
}

/** `trail` ending at `end` (where the pointer was let go), as a path fit for the protocol:
 * every point on the picture of `width` by `height` (a pointer dragged past its edge means
 * the edge, as a finger cannot leave a screen), times whole and never decreasing, and at
 * most PATH_MAX points, thinned evenly with the first and last always kept. */
export function path(
  trail: TrailPoint[],
  end: TrailPoint,
  size: { width: number; height: number },
): TrailPoint[] {
  const all = [...trail, end];
  const count = Math.min(all.length, PATH_MAX);
  const points: TrailPoint[] = [];
  let t = 0;
  for (let i = 0; i < count; i += 1) {
    const p = all[Math.round((i * (all.length - 1)) / Math.max(1, count - 1))];
    t = Math.max(t, Math.round(p.t_ms));
    points.push({
      x: Math.min(size.width - 1, Math.max(0, Math.round(p.x))),
      y: Math.min(size.height - 1, Math.max(0, Math.round(p.y))),
      t_ms: t,
    });
  }
  return points;
}

/** The swipe a drag is sent as. Its path is the way the pointer took when that way is
 * more than a line (three points or more) and ran within the duration the protocol takes;
 * otherwise the swipe is the straight line it always was, in the time the drag took. */
export function swipeOf(
  trail: TrailPoint[],
  end: TrailPoint,
  size: { width: number; height: number },
): {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  duration_ms: number;
  path?: TrailPoint[];
} {
  const points = path(trail, end, size);
  const first = points[0];
  const last = points[points.length - 1];
  const duration_ms = Math.min(
    DURATION_MAX_MS,
    Math.max(DURATION_MIN_MS, last.t_ms),
  );
  const curved =
    points.length >= 3 &&
    last.t_ms >= DURATION_MIN_MS &&
    last.t_ms <= DURATION_MAX_MS;
  return {
    x1: first.x,
    y1: first.y,
    x2: last.x,
    y2: last.y,
    duration_ms,
    ...(curved ? { path: points } : {}),
  };
}
