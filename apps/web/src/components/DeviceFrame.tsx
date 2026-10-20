import type { ActionView, LiveFrame, ObservationView } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/format";
import { Screenshot } from "./Screenshot";

/** Marks are drawn at this size on a screen this many pixels wide, scaled to the shot. */
const BASE = 390;

export interface InputMark {
  kind: ActionView["kind"];
  input: Record<string, unknown>;
}

/** Draws the agent's input on the screenshot, in the screenshot's pixel coordinates. */
export function InputOverlay({
  mark,
  w,
  h,
}: {
  mark: InputMark;
  w: number;
  h: number;
}) {
  const i = mark.input as Record<string, number>;
  const s = w / BASE;
  const ring = (x: number, y: number) => (
    <g>
      <circle
        cx={x}
        cy={y}
        r={22 * s}
        fill="none"
        stroke="white"
        strokeWidth={6 * s}
        opacity={0.9}
      />
      <circle
        cx={x}
        cy={y}
        r={22 * s}
        fill="rgb(79 70 229 / 0.14)"
        stroke="#4F46E5"
        strokeWidth={2.5 * s}
      />
      <circle
        cx={x}
        cy={y}
        r={3.5 * s}
        fill="#4F46E5"
        stroke="white"
        strokeWidth={1.5 * s}
      />
    </g>
  );
  // A second ring around the point: dashed for a finger that stays (a long press, the
  // end of a held swipe), solid for a second tap.
  const halo = (x: number, y: number, r: number, held: boolean) => (
    <g fill="none">
      <circle
        cx={x}
        cy={y}
        r={r}
        stroke="white"
        strokeWidth={5 * s}
        opacity={0.85}
      />
      <circle
        cx={x}
        cy={y}
        r={r}
        stroke="#4F46E5"
        strokeWidth={2.5 * s}
        strokeDasharray={held ? `${6 * s} ${4.5 * s}` : undefined}
      />
    </g>
  );
  let body = null;
  if (
    (mark.kind === "tap" ||
      mark.kind === "set_picker" ||
      mark.kind === "select_option") &&
    i.x != null
  ) {
    body = ring(i.x, i.y);
  } else if (
    (mark.kind === "long_press" || mark.kind === "double_tap") &&
    i.x != null
  ) {
    body = (
      <g>
        {halo(i.x, i.y, 32 * s, mark.kind === "long_press")}
        {ring(i.x, i.y)}
      </g>
    );
  } else if (mark.kind === "pinch" && i.x != null) {
    // The point zoomed around, and the two fingers: apart to zoom in, together to zoom out.
    const [from, to] = i.scale > 1 ? [32 * s, 72 * s] : [72 * s, 32 * s];
    const head = 10 * s;
    const finger = (side: number) => {
      const x1 = i.x + side * from;
      const x2 = i.x + side * to;
      const back = x2 - Math.sign(x2 - x1) * head;
      const tip = `${back},${i.y - head} ${x2},${i.y} ${back},${i.y + head}`;
      return (
        <g key={side} fill="none">
          <line
            x1={x1}
            y1={i.y}
            x2={x2}
            y2={i.y}
            stroke="white"
            strokeWidth={8 * s}
            opacity={0.85}
          />
          <polyline
            points={tip}
            stroke="white"
            strokeWidth={8 * s}
            opacity={0.85}
          />
          <line
            x1={x1}
            y1={i.y}
            x2={x2}
            y2={i.y}
            stroke="#4F46E5"
            strokeWidth={3 * s}
          />
          <polyline points={tip} stroke="#4F46E5" strokeWidth={3 * s} />
        </g>
      );
    };
    body = (
      <g strokeLinecap="round" strokeLinejoin="round">
        {ring(i.x, i.y)}
        {finger(-1)}
        {finger(1)}
      </g>
    );
  } else if (mark.kind === "swipe" && i.x1 != null) {
    const angle = Math.atan2(i.y2 - i.y1, i.x2 - i.x1);
    const head = 16 * s;
    const p = (a: number) =>
      `${i.x2 - head * Math.cos(angle - a)},${i.y2 - head * Math.sin(angle - a)}`;
    body = (
      <g strokeLinecap="round" strokeLinejoin="round">
        {i.hold_ms > 0 && halo(i.x2, i.y2, 20 * s, true)}
        <line
          x1={i.x1}
          y1={i.y1}
          x2={i.x2}
          y2={i.y2}
          stroke="white"
          strokeWidth={8 * s}
          opacity={0.85}
        />
        <line
          x1={i.x1}
          y1={i.y1}
          x2={i.x2}
          y2={i.y2}
          stroke="#4F46E5"
          strokeWidth={3 * s}
        />
        <circle
          cx={i.x1}
          cy={i.y1}
          r={7 * s}
          fill="white"
          stroke="#4F46E5"
          strokeWidth={2.5 * s}
        />
        <polyline
          points={`${p(0.5)} ${i.x2},${i.y2} ${p(-0.5)}`}
          fill="none"
          stroke="#4F46E5"
          strokeWidth={3 * s}
        />
      </g>
    );
  } else if (mark.kind === "home") {
    body = (
      <rect
        x={w / 2 - 70 * s}
        y={h - 14 * s}
        width={140 * s}
        height={8 * s}
        rx={4 * s}
        fill="#4F46E5"
        stroke="white"
        strokeWidth={2 * s}
      />
    );
  }
  if (!body) return null;
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden
    >
      {body}
    </svg>
  );
}

/** How wide a thumbnail in the conversation is drawn, and the picture asked for it
 * (about 2.5 times wider, for sharp pixels on dense screens). */
export const THUMB_CLASS = "w-[92px]";
/** A landscape screen's thumbnail: twice as wide, so it is not a sliver. */
export const THUMB_CLASS_WIDE = "w-[184px]";

/** The thumbnail width for a screenshot: wider when the phone was held sideways. */
export function thumbClass(obs: ObservationView | null | undefined) {
  return obs?.width && obs.height && obs.width > obs.height
    ? THUMB_CLASS_WIDE
    : THUMB_CLASS;
}
const THUMB_PIXELS = 240;

/** A real screenshot in a minimal iPhone or Android bezel, at the screen's true shape. */
export function DeviceFrame({
  platform,
  obs,
  live,
  mark,
  label,
  onClick,
  empty,
  className,
  thumb,
}: {
  platform: "ios" | "android" | "fake";
  obs: ObservationView | null | undefined;
  /** A live picture of the screen, shown instead of the screenshot (no input mark). */
  live?: LiveFrame | null;
  mark?: InputMark | null;
  label: string;
  onClick?: () => void;
  empty?: string;
  className?: string;
  /** A thumbnail: a small copy of the screenshot is loaded, not the whole picture. */
  thumb?: boolean;
}) {
  const { t } = useI18n();
  const ios = platform !== "android";
  const w = live?.width ?? obs?.width ?? (ios ? 1206 : 1080);
  const h = live?.height ?? obs?.height ?? (ios ? 2622 : 2400);
  const ready = obs?.status === "complete";
  // Held sideways the corners turn with the phone, and the bezel stays as thick as
  // in portrait (padding in % is of the width, so it is scaled by the shape).
  const landscape = w > h;
  const Wrapper = onClick ? "button" : "div";
  // One name for the whole picture; the image inside it is not announced again.
  const name = mark ? t("frame.marked", { label }) : label;
  return (
    <Wrapper
      type={onClick ? "button" : undefined}
      onClick={onClick}
      aria-label={onClick ? name : undefined}
      className={cn(
        "relative block w-full select-none",
        onClick &&
          "cursor-zoom-in transition-transform duration-150 ease-out hover:-translate-y-px",
        onClick && (landscape ? "rounded-[6.5%/14%]" : "rounded-[14%/6.5%]"),
        className,
      )}
    >
      <div
        style={{ padding: `${landscape ? (3.4 * h) / w : 3.4}%` }}
        className={cn(
          "relative bg-[#16171a] shadow-[0_0_0_1px_rgb(255_255_255/0.07)_inset,0_0_0_1px_var(--line-strong)]",
          ios
            ? landscape
              ? "rounded-[7%/15%]"
              : "rounded-[15%/7%]"
            : landscape
              ? "rounded-[5%/11%]"
              : "rounded-[11%/5%]",
        )}
      >
        <div
          role={onClick ? undefined : "img"}
          aria-label={onClick ? undefined : name}
          style={{ aspectRatio: `${w} / ${h}` }}
          className={cn(
            "relative overflow-hidden bg-black",
            ios
              ? landscape
                ? "rounded-[5.6%/12%]"
                : "rounded-[12%/5.6%]"
              : landscape
                ? "rounded-[3.9%/8.5%]"
                : "rounded-[8.5%/3.9%]",
          )}
        >
          {live ? (
            <img
              src={`data:image/jpeg;base64,${live.jpeg}`}
              alt=""
              draggable={false}
              className="absolute inset-0 h-full w-full object-cover"
            />
          ) : obs && ready ? (
            <Screenshot
              observationId={obs.id}
              alt=""
              width={thumb ? THUMB_PIXELS : undefined}
              className="absolute inset-0 h-full w-full"
            />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center bg-[#1c1d21] p-4 text-center text-[12px] text-[#8b8e96]">
              {empty}
            </div>
          )}
          {!live && obs && ready && mark && (
            <InputOverlay mark={mark} w={w} h={h} />
          )}
        </div>
      </div>
    </Wrapper>
  );
}

/** Shape-only device silhouette for device cards. */
export function DeviceSilhouette({
  platform,
  muted,
}: {
  platform: "ios" | "android" | "fake";
  muted?: boolean;
}) {
  const ios = platform !== "android";
  return (
    <div
      aria-hidden
      className={cn(
        "relative w-[36px] shrink-0 border-[1.5px] p-[3px]",
        ios ? "rounded-[9px]" : "rounded-[6px]",
        muted ? "border-line-strong" : "border-fg/70",
      )}
      style={{ aspectRatio: "9 / 19.5" }}
    >
      <div
        className={cn(
          "h-full w-full bg-sunken",
          ios ? "rounded-[6px]" : "rounded-[3px]",
        )}
      />
    </div>
  );
}
