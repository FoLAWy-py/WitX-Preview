import { useEffect, useState } from "react";
import { cn } from "@/lib/format";

/** Whether a scroller has content out of sight above and below what it shows: as it
 * scrolls, as it is resized, and as its content changes. `ref` goes on the scroller,
 * which may come and go (a sidebar folded and unfolded). */
export function useScrollEdges(): {
  ref: (el: HTMLElement | null) => void;
  above: boolean;
  below: boolean;
} {
  const [el, ref] = useState<HTMLElement | null>(null);
  const [edges, setEdges] = useState({ above: false, below: false });
  useEffect(() => {
    if (!el) return;
    const measure = () => {
      const above = el.scrollTop > 1;
      const below = el.scrollHeight - el.scrollTop - el.clientHeight > 1;
      setEdges((e) =>
        e.above === above && e.below === below ? e : { above, below },
      );
    };
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    const resized = new ResizeObserver(measure);
    resized.observe(el);
    const changed = new MutationObserver(measure);
    changed.observe(el, { childList: true, subtree: true });
    return () => {
      el.removeEventListener("scroll", measure);
      resized.disconnect();
      changed.disconnect();
    };
  }, [el]);
  return { ref, ...edges };
}

/** A soft edge where content scrolls out of sight next to a fixed bar: shown only
 * while there is content on that side, so nothing at rest is veiled. Its parent is
 * positioned, and the page behind is `bg`. */
export function Fade({
  side,
  show,
}: {
  side: "top" | "bottom";
  show: boolean;
}) {
  return (
    <div
      aria-hidden
      className={cn(
        "pointer-events-none absolute inset-x-0 z-[1] h-6 from-bg to-transparent transition-opacity duration-200",
        side === "top" ? "top-0 bg-linear-to-b" : "bottom-0 bg-linear-to-t",
        show ? "opacity-100" : "opacity-0",
      )}
    />
  );
}
