import {
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent,
  type RefObject,
} from "react";
import type { TakeoverFrame } from "@/api/types.gen";
import { base64Bytes } from "@/lib/frame";
import { gestureOf, sameGeometry, TAP_SLOP, wheelSwipeOf } from "@/lib/gesture";
import { sample, type TrailPoint } from "@/lib/trail";

/** The trail of a drag on the picture: its width in page pixels, and its colour when the
 * page's accent cannot be read. */
const TRAIL_WIDTH = 3;
const TRAIL_FALLBACK = "#4f46e5";

export type Point = { x: number; y: number };

/**
 * The phone's screen in a takeover: the pictures that come in, drawn on `canvas`, and
 * what the pointer does on them. A click is a tap, a held click a long press and a drag
 * a swipe, sent with the way the pointer took; that way is drawn on the picture as
 * it is made, until the next picture arrives. The mouse wheel scrolls the phone.
 *
 * `send` sends an input against a picture and returns its ID; `pressed` shows where a
 * press landed; `turned` says a drag was not sent because the phone turned meanwhile.
 */
export function useScreen({
  canvas,
  ended,
  send,
  pressed,
  turned,
}: {
  canvas: RefObject<HTMLCanvasElement | null>;
  ended: boolean;
  send: (input: Record<string, unknown>, frameId: number) => string;
  pressed: (at: Point, input: string) => void;
  turned: () => void;
}) {
  const press = useRef<{
    at: Point;
    frame: TakeoverFrame;
    start: number; // the press's timeStamp; the trail's times are relative to it
    trail: TrailPoint[]; // where the pointer has been, in pixels of `frame`
  } | null>(null);
  // `frame` changes only when the picture's size does (and for the first one): the
  // pictures themselves are drawn straight onto the canvas, not through React.
  const [frame, setFrame] = useState<TakeoverFrame>();
  // The picture on the canvas: input refers to it by frame_id.
  const shown = useRef<TakeoverFrame>(undefined);
  const bitmap = useRef<ImageBitmap>(undefined);
  // The newest picture that arrived while one was being decoded; older ones are skipped.
  const waiting = useRef<TakeoverFrame>(undefined);
  const decoding = useRef(false);
  const lastPicture = useRef(Date.now());

  /** Draws the picture once it is decoded. Decoding happens off the page's main thread;
   * a picture that arrives meanwhile replaces the one waiting, so the screen never falls
   * behind the phone. */
  async function show(message: TakeoverFrame) {
    waiting.current = message;
    if (decoding.current) return;
    decoding.current = true;
    try {
      while (waiting.current) {
        const next = waiting.current;
        waiting.current = undefined;
        try {
          const decoded = await createImageBitmap(
            new Blob([base64Bytes(next.jpeg)], { type: "image/jpeg" }),
          );
          bitmap.current?.close();
          bitmap.current = decoded;
        } catch {
          continue; // a picture that cannot be decoded is skipped
        }
        shown.current = next;
        lastPicture.current = Date.now();
        paint();
        setFrame((was) =>
          was && was.width === next.width && was.height === next.height
            ? was
            : next,
        );
      }
    } finally {
      decoding.current = false;
    }
  }

  /** Draws the picture, and over it the trail of a drag still under way. A drag that
   * ended leaves its trail on the picture until the next one is drawn. */
  function paint() {
    const el = canvas.current;
    const picture = bitmap.current;
    const on = shown.current;
    if (!el || !picture || !on) return;
    if (el.width !== on.width) el.width = on.width;
    if (el.height !== on.height) el.height = on.height;
    const context = el.getContext("2d");
    if (!context) return;
    context.drawImage(picture, 0, 0, el.width, el.height);
    const trail = press.current?.trail;
    if (trail && trail.length > 1) drawTrail(el, context, trail);
  }

  // The canvas appears with the first picture, and is cleared when its size changes.
  useLayoutEffect(paint, [frame]);

  // The decoded picture holds memory outside the page's heap until it is closed.
  useEffect(() => () => bitmap.current?.close(), []);

  /** Page coordinates on the picture -> pixels of the frame it shows. */
  function toFrame(page: Point, on: TakeoverFrame): Point {
    const el = canvas.current!;
    const box = el.getBoundingClientRect();
    const left = box.left + el.clientLeft;
    const top = box.top + el.clientTop;
    return {
      x: Math.round(((page.x - left) * on.width) / el.clientWidth),
      y: Math.round(((page.y - top) * on.height) / el.clientHeight),
    };
  }

  function onPointerDown(event: PointerEvent) {
    const on = shown.current;
    if (!on || ended) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const at = { x: event.clientX, y: event.clientY };
    press.current = {
      at,
      frame: on,
      start: event.timeStamp,
      trail: [{ ...toFrame(at, on), t_ms: 0 }],
    };
  }

  /** While pressed, the pointer's way is kept (one point every SAMPLE_MS or so) and drawn. */
  function onPointerMove(event: PointerEvent) {
    const started = press.current;
    if (!started) return;
    const here = toFrame({ x: event.clientX, y: event.clientY }, started.frame);
    const t_ms = Math.round(event.timeStamp - started.start);
    if (sample(started.trail, { ...here, t_ms })) paint();
  }

  /** A gesture is sent against the newest picture, not the one it began on: a slow drag
   * outlives the frames the server keeps. Its coordinates are still those of the
   * picture it began on, which is the same thing while the screen's geometry is the
   * same; a phone turned round meanwhile makes them wrong, so then nothing is sent and
   * the person is asked to drag again. */
  function onPointerUp(event: PointerEvent) {
    const started = press.current;
    if (!started) return;
    const from = toFrame(started.at, started.frame);
    const to = toFrame({ x: event.clientX, y: event.clientY }, started.frame);
    const moved = Math.hypot(
      event.clientX - started.at.x,
      event.clientY - started.at.y,
    );
    const held = Math.round(event.timeStamp - started.start);
    const on = shown.current ?? started.frame;
    if (!sameGeometry(on, started.frame)) {
      press.current = null;
      paint(); // the trail of a drag that is not sent goes
      turned();
      return;
    }
    const gesture = gestureOf({
      from,
      to,
      moved,
      held,
      trail: started.trail,
      size: started.frame,
    });
    if (moved >= TAP_SLOP) {
      // The whole way, to where it was let go, stays on the picture until the next one.
      started.trail.push({ ...to, t_ms: held });
      paint();
    }
    press.current = null;
    pressed(started.at, send(gesture, on.frame_id));
  }

  // The mouse wheel scrolls the phone: the turns of one gesture become one swipe, the
  // way a finger would move (wheel down: the finger goes up).
  const wheelSwipe = useEffectEvent((total: number, at: Point) => {
    const on = shown.current;
    const el = canvas.current;
    if (!on || !el || !total) return;
    send(
      wheelSwipeOf(total, toFrame(at, on), on, el.clientHeight),
      on.frame_id,
    );
  });
  const showing = frame !== undefined;
  useEffect(() => {
    const el = canvas.current;
    if (!el || !showing || ended) return;
    let total = 0;
    let at: Point = { x: 0, y: 0 };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      total += e.deltaY;
      at = { x: e.clientX, y: e.clientY };
      clearTimeout(timer);
      timer = setTimeout(() => {
        wheelSwipe(total, at);
        total = 0;
      }, 120);
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => {
      el.removeEventListener("wheel", wheel);
      clearTimeout(timer);
    };
  }, [canvas, showing, ended]);

  return {
    /** The picture's size and the first picture (see above); undefined until it comes. */
    frame,
    /** The picture on the canvas now, which input refers to. */
    shown,
    /** When the last picture was drawn. */
    lastPicture,
    show,
    pointer: { onPointerDown, onPointerMove, onPointerUp },
  };
}

/** The way a drag has taken, on the picture: a line in the page's accent colour with a
 * light edge, so it shows on a dark screen and a light one alike. The picture's pixels
 * are larger than the page's, so the width is scaled to look the same at any size. */
function drawTrail(
  canvas: HTMLCanvasElement,
  context: CanvasRenderingContext2D,
  trail: TrailPoint[],
) {
  const scale = canvas.width / Math.max(1, canvas.clientWidth);
  const accent =
    getComputedStyle(canvas).getPropertyValue("--accent").trim() ||
    TRAIL_FALLBACK;
  context.save();
  context.lineCap = "round";
  context.lineJoin = "round";
  context.beginPath();
  context.moveTo(trail[0].x, trail[0].y);
  for (const point of trail.slice(1)) context.lineTo(point.x, point.y);
  context.strokeStyle = "rgba(255, 255, 255, 0.85)";
  context.lineWidth = (TRAIL_WIDTH + 2) * scale;
  context.stroke();
  context.strokeStyle = accent;
  context.lineWidth = TRAIL_WIDTH * scale;
  context.stroke();
  context.restore();
}
