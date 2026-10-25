import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { CircleHelp, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/format";
import { useLayout } from "@/lib/prefs";
import { Ico } from "./base";
import { Sheet, useLayer } from "./dialog";

export type Placement = "bottom-start" | "bottom-end" | "top-start" | "top-end";

/**
 * A floating panel anchored to its trigger. It opens below (above when there is no
 * room), stays inside the window, follows scrolling, closes on an outside press or
 * Escape and gives focus back to the trigger. On phones it is a bottom sheet.
 */
export function Popover({
  anchor,
  open,
  onClose,
  placement = "bottom-start",
  title,
  minWidth = 220,
  children,
}: {
  anchor: RefObject<HTMLElement | null>;
  open: boolean;
  onClose: () => void;
  placement?: Placement;
  title: string;
  minWidth?: number;
  children: ReactNode;
}) {
  const { layout } = useLayout();
  const panel = useLayer(
    open && layout !== "phone",
    () => {
      onClose();
      anchor.current?.focus();
    },
    false,
  );
  const [style, setStyle] = useState<React.CSSProperties>({
    visibility: "hidden",
  });

  const place = useCallback(() => {
    const a = anchor.current?.getBoundingClientRect();
    const p = panel.current;
    if (!a || !p) return;
    const gap = 6;
    const margin = 8;
    const width = Math.max(minWidth, a.width);
    const height = p.offsetHeight;
    const below = window.innerHeight - a.bottom - gap - margin;
    const above = a.top - gap - margin;
    const up = placement.startsWith("top")
      ? !(above < height && below > above)
      : below < height && above > below;
    const top = up ? Math.max(margin, a.top - gap - height) : a.bottom + gap;
    const left = placement.endsWith("end") ? a.right - width : a.left;
    setStyle({
      position: "fixed",
      top,
      left: Math.min(
        Math.max(margin, left),
        window.innerWidth - width - margin,
      ),
      width,
      maxHeight: (up ? above : below) || undefined,
    });
  }, [anchor, minWidth, placement]);

  useLayoutEffect(() => {
    if (!open || layout === "phone") return;
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, layout, place]);

  useEffect(() => {
    if (!open || layout === "phone") return;
    const down = (e: MouseEvent) => {
      const target = e.target as Node;
      if (panel.current?.contains(target) || anchor.current?.contains(target))
        return;
      onClose();
    };
    document.addEventListener("mousedown", down);
    return () => document.removeEventListener("mousedown", down);
  }, [open, layout, onClose, anchor]);

  if (layout === "phone") {
    return (
      <Sheet open={open} onClose={onClose} title={title}>
        <div className="p-2">{children}</div>
      </Sheet>
    );
  }
  if (!open) return null;
  return createPortal(
    <div
      ref={panel}
      style={style}
      className="anim-pop elev scroll-quiet z-[90] overflow-auto rounded-xl border border-line bg-surface p-1"
    >
      {children}
    </div>,
    document.body,
  );
}

/** Arrow keys, Home and End move focus between the items of a list in a popover. */
export function moveFocus(
  e: KeyboardEvent<HTMLElement>,
  root: HTMLElement | null,
) {
  const items = Array.from(
    root?.querySelectorAll<HTMLElement>(
      "[data-item]:not([aria-disabled=true])",
    ) ?? [],
  );
  if (!items.length) return;
  const at = items.indexOf(document.activeElement as HTMLElement);
  let next = -1;
  if (e.key === "ArrowDown") next = (at + 1) % items.length;
  else if (e.key === "ArrowUp") next = (at - 1 + items.length) % items.length;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = items.length - 1;
  if (next < 0) return;
  e.preventDefault();
  items[next].focus();
}

export function useFirstItemFocus(open: boolean) {
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => {
      const items = list.current?.querySelectorAll<HTMLElement>(
        "[data-item]:not([aria-disabled=true])",
      );
      const chosen = list.current?.querySelector<HTMLElement>(
        "[data-item][aria-selected=true], [data-item][aria-checked=true]",
      );
      (chosen ?? items?.[0])?.focus();
    }, 0);
    return () => clearTimeout(timer);
  }, [open]);
  return list;
}

export const itemClass =
  "flex w-full cursor-pointer items-start gap-2.5 rounded-lg px-2.5 py-2 text-left text-[14px] outline-none hover:bg-sunken focus-visible:bg-sunken focus-visible:outline-none aria-disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:hover:bg-transparent pointer-coarse:min-h-11";

export type MenuEntry =
  | {
      id: string;
      label: string;
      icon?: LucideIcon;
      hint?: string;
      danger?: boolean;
      disabled?: boolean;
      onSelect: () => void;
    }
  | { separator: true; id: string }
  | { header: string; id: string };

/** A button that opens a list of actions. */
export function Menu({
  label,
  entries,
  placement = "bottom-end",
  trigger,
  minWidth,
}: {
  label: string;
  entries: MenuEntry[];
  placement?: Placement;
  trigger: (props: {
    ref: RefObject<HTMLButtonElement | null>;
    open: boolean;
    toggle: () => void;
    onKeyDown: (e: KeyboardEvent<HTMLButtonElement>) => void;
  }) => ReactNode;
  minWidth?: number;
}) {
  const anchor = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const list = useFirstItemFocus(open);
  const close = useCallback(() => setOpen(false), []);
  return (
    <>
      {trigger({
        ref: anchor,
        open,
        toggle: () => setOpen((o) => !o),
        onKeyDown: (e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            setOpen(true);
          }
        },
      })}
      <Popover
        anchor={anchor}
        open={open}
        onClose={close}
        placement={placement}
        title={label}
        minWidth={minWidth}
      >
        <div
          ref={list}
          role="menu"
          aria-label={label}
          onKeyDown={(e) => {
            moveFocus(e, list.current);
            if (e.key === "Tab") close();
          }}
        >
          {entries.map((entry) =>
            "separator" in entry ? (
              <div
                key={entry.id}
                role="separator"
                className="my-1 h-px bg-line"
              />
            ) : "header" in entry ? (
              <div
                key={entry.id}
                className="px-2.5 pt-2 pb-1 text-[12px] font-medium text-faint"
              >
                {entry.header}
              </div>
            ) : (
              <button
                key={entry.id}
                type="button"
                role="menuitem"
                data-item
                aria-disabled={entry.disabled || undefined}
                onClick={() => {
                  if (entry.disabled) return;
                  close();
                  anchor.current?.focus();
                  entry.onSelect();
                }}
                className={cn(itemClass, entry.danger && "text-red")}
              >
                {entry.icon && (
                  <Ico icon={entry.icon} size={16} className="mt-0.5" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block">{entry.label}</span>
                  {entry.hint && (
                    <span className="block text-[12.5px] text-muted">
                      {entry.hint}
                    </span>
                  )}
                </span>
              </button>
            ),
          )}
        </div>
      </Popover>
    </>
  );
}

/** A "?" beside a heading that opens a few lines about the page, kept out of the way. */
export function InfoTip({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const anchor = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-label={label}
        title={label}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-faint hover:bg-sunken hover:text-fg aria-expanded:bg-sunken aria-expanded:text-fg pointer-coarse:h-9 pointer-coarse:w-9"
      >
        <CircleHelp size={15} strokeWidth={1.5} aria-hidden />
      </button>
      <Popover
        anchor={anchor}
        open={open}
        onClose={close}
        title={label}
        minWidth={320}
      >
        <div className="px-2.5 py-2 text-[13.5px] leading-relaxed text-muted">
          {children}
        </div>
      </Popover>
    </>
  );
}
