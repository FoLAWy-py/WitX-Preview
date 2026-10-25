import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/format";
import { Button } from "./base";

/**
 * Keeps something on screen while it animates out: `shown` stays true for `ms` after
 * `open` turns false, with `leaving` set meanwhile (no wait with reduced motion).
 */
export function usePresence(open: boolean, ms: number) {
  const [shown, setShown] = useState(open);
  if (open && !shown) setShown(true);
  useEffect(() => {
    if (open || !shown) return;
    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const timer = setTimeout(() => setShown(false), reduce ? 0 : ms);
    return () => clearTimeout(timer);
  }, [open, shown, ms]);
  return { shown: open || shown, leaving: !open && shown };
}

/**
 * Dragging something away to close it: down for a sheet, left for a drawer. `offset`
 * is how far it has been dragged (never the other way); let go past a third of its
 * size, or with a flick, and it closes; otherwise it springs back.
 */
export function useDragToClose(
  direction: "down" | "left",
  onClose: (() => void) | undefined,
) {
  const start = useRef<{ x: number; y: number; t: number; size: number }>(null);
  const [offset, setOffset] = useState(0);
  // A ref as well as state: pointer events can arrive before React re-renders, and
  // a release must see the drag that the moves before it started.
  const draggingNow = useRef(false);
  const [dragging, setDragging] = useState(false);
  const travel = (e: PointerEvent<HTMLElement>, s: { x: number; y: number }) =>
    direction === "down" ? e.clientY - s.y : s.x - e.clientX;
  const handlers = {
    onPointerDown(e: PointerEvent<HTMLElement>) {
      if (!onClose || (e.pointerType === "mouse" && e.button !== 0)) return;
      const panel = e.currentTarget.closest<HTMLElement>("[data-drag-panel]");
      const box = (panel ?? e.currentTarget).getBoundingClientRect();
      start.current = {
        x: e.clientX,
        y: e.clientY,
        t: e.timeStamp,
        size: direction === "down" ? box.height : box.width,
      };
    },
    onPointerMove(e: PointerEvent<HTMLElement>) {
      if (!start.current) return;
      const moved = travel(e, start.current);
      if (!draggingNow.current && Math.abs(moved) < 6) return;
      if (!draggingNow.current) {
        e.currentTarget.setPointerCapture(e.pointerId);
        draggingNow.current = true;
        setDragging(true);
      }
      setOffset(Math.max(0, moved));
    },
    onPointerUp(e: PointerEvent<HTMLElement>) {
      const s = start.current;
      start.current = null;
      if (!s || !draggingNow.current) return;
      draggingNow.current = false;
      setDragging(false);
      const moved = Math.max(0, travel(e, s));
      const speed = moved / Math.max(1, e.timeStamp - s.t); // px per ms
      if (moved > s.size / 3 || (speed > 0.5 && moved > 24)) onClose?.();
      else setOffset(0);
    },
    onPointerCancel() {
      start.current = null;
      draggingNow.current = false;
      setDragging(false);
      setOffset(0);
    },
  };
  /** Where the panel sits now; a release springs back with the sheet curve. */
  const style = (axis: "X" | "Y"): CSSProperties =>
    ({
      "--drag": `${offset}px`,
      transform: offset
        ? `translate${axis}(${axis === "X" ? -offset : offset}px)`
        : undefined,
      transition: dragging ? "none" : "transform 300ms var(--ease-sheet)",
    }) as CSSProperties;
  const reset = useCallback(() => setOffset(0), []);
  return { handlers, style, reset };
}

/** An open dialog, sheet, drawer or popover; the last one opened is on top. */
interface Layer {
  panel: RefObject<HTMLElement | null>;
  close: RefObject<(() => void) | undefined>;
  /** Focus stays inside it while it is on top. */
  modal: boolean;
}

const layers: Layer[] = [];

const FOCUSABLE =
  "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

function focusables(panel: HTMLElement) {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.getClientRects().length > 0,
  );
}

/** Escape closes only the layer on top, and Tab goes round inside it if it is modal.
 * A key something inside has already handled (`preventDefault`) is left alone. */
function onLayerKey(e: globalThis.KeyboardEvent) {
  const top = layers.at(-1);
  if (!top || e.defaultPrevented) return;
  if (e.key === "Escape") {
    const close = top.close.current;
    if (!close) return;
    e.preventDefault();
    close();
    return;
  }
  const panel = top.panel.current;
  if (e.key !== "Tab" || !top.modal || !panel) return;
  const items = focusables(panel);
  const at = document.activeElement;
  const inside = panel.contains(at);
  const edge = e.shiftKey ? items[0] : items.at(-1);
  const wrap = () => (e.shiftKey ? items.at(-1) : items[0])?.focus();
  if (inside && at !== edge && items.length) {
    // Safari's Tab skips buttons and links, so the last field can be its last stop
    // here and the next Tab leaves: then focus goes round as it would from the edge.
    setTimeout(() => {
      if (layers.at(-1) === top && !panel.contains(document.activeElement))
        wrap();
    });
    return;
  }
  e.preventDefault();
  wrap();
}

/**
 * Puts a layer on the stack while `open`. A modal layer takes focus when it opens (an
 * element marked `data-autofocus` first) and gives it back when it closes.
 */
export function useLayer<T extends HTMLElement = HTMLDivElement>(
  open: boolean,
  onClose: (() => void) | undefined,
  modal = true,
) {
  const panel = useRef<T>(null);
  const close = useRef(onClose);
  useLayoutEffect(() => {
    close.current = onClose;
  });
  useEffect(() => {
    if (!open) return;
    const layer: Layer = { panel, close, modal };
    layers.push(layer);
    if (layers.length === 1) window.addEventListener("keydown", onLayerKey);
    const prev = modal ? (document.activeElement as HTMLElement | null) : null;
    if (modal && panel.current) {
      const first =
        panel.current.querySelector<HTMLElement>("[data-autofocus]") ??
        focusables(panel.current)[0];
      first?.focus();
    }
    return () => {
      layers.splice(layers.indexOf(layer), 1);
      if (!layers.length) window.removeEventListener("keydown", onLayerKey);
      prev?.focus?.();
    };
  }, [open, modal]);
  return panel;
}

/** Centered dialog. Without `onClose` it cannot be dismissed by backdrop or Escape. */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  tone,
  wide,
}: {
  open: boolean;
  onClose?: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  tone?: "amber" | "red" | "violet";
  wide?: boolean;
}) {
  const ref = useLayer(open, onClose);
  const { t } = useI18n();
  const [id] = useState(() => `dialog-${Math.random().toString(36).slice(2)}`);
  const { shown, leaving } = usePresence(open, 150);
  if (!shown) return null;
  return createPortal(
    <div
      className={cn(
        "fixed inset-0 z-[80] flex items-center justify-center bg-black/35 p-4 dark:bg-black/60",
        leaving ? "anim-fade-out pointer-events-none" : "anim-fade",
      )}
      onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal
        aria-labelledby={id}
        className={cn(
          "elev max-h-[92dvh] w-full overflow-auto rounded-xl border bg-surface",
          leaving ? "anim-dialog-out" : "anim-dialog",
          wide ? "max-w-2xl" : "max-w-md",
          tone === "amber"
            ? "border-amber/60"
            : tone === "red"
              ? "border-red/50"
              : tone === "violet"
                ? "border-violet/50"
                : "border-line",
        )}
      >
        <div className="flex items-start gap-3 px-5 pt-5">
          <h2 id={id} className="flex-1 text-[17px] font-semibold">
            {title}
          </h2>
          {onClose && (
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              icon={X}
              aria-label={t("common.close")}
              onClick={onClose}
            />
          )}
        </div>
        <div className="px-5 py-3 text-[14px] text-muted">{children}</div>
        {footer && (
          <div className="flex flex-wrap justify-end gap-2 px-5 pt-2 pb-5">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** A sheet from the bottom of the screen (phones). */
export function Sheet({
  open,
  onClose,
  title,
  children,
  full,
  tone,
}: {
  open: boolean;
  onClose?: () => void;
  title: ReactNode;
  children: ReactNode;
  full?: boolean;
  tone?: "amber";
}) {
  const ref = useLayer(open, onClose);
  const { t } = useI18n();
  const { shown, leaving } = usePresence(open, 240);
  const drag = useDragToClose("down", full ? undefined : onClose);
  const { reset } = drag;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);
  if (!shown) return null;
  return createPortal(
    <div
      className={cn(
        "fixed inset-0 z-[80] flex items-end bg-black/40 dark:bg-black/65",
        leaving ? "anim-fade-out pointer-events-none" : "anim-fade",
      )}
      onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal
        aria-label={typeof title === "string" ? title : undefined}
        data-drag-panel
        style={drag.style("Y")}
        className={cn(
          "flex w-full flex-col overflow-hidden rounded-t-2xl border-t bg-surface pb-[env(safe-area-inset-bottom)]",
          leaving ? "anim-sheet-out" : "anim-sheet",
          full ? "h-[100dvh] rounded-t-none" : "max-h-[90dvh]",
          tone === "amber" ? "border-amber" : "border-line",
        )}
      >
        {/* The handle and the title bar drag the sheet down to close it. */}
        <div
          {...drag.handlers}
          className={cn(
            "relative flex min-h-14 items-center gap-2 border-b border-line px-4",
            !full && onClose && "touch-none pt-2",
          )}
        >
          {!full && onClose && (
            <span
              aria-hidden
              className="absolute top-1.5 left-1/2 h-1 w-9 -translate-x-1/2 rounded-full bg-line-strong"
            />
          )}
          <div className="flex-1 text-[16px] font-semibold">{title}</div>
          {onClose && (
            <Button
              variant="ghost"
              iconOnly
              icon={X}
              aria-label={t("common.close")}
              onClick={onClose}
            />
          )}
        </div>
        <div className="scroll-quiet flex-1 overflow-auto">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
