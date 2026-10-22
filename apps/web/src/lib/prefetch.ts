/** Loads some code: a page, or the Markdown formatter. */
export type Load = () => Promise<unknown>;

const INTENT = ["pointerover", "pointerdown", "focusin"] as const;

/** Whether the browser asks to use less data (Save-Data). */
function savingData(): boolean {
  const { connection } = navigator as Navigator & {
    connection?: { saveData?: boolean };
  };
  return connection?.saveData === true;
}

/**
 * Starts loading a page's code as soon as the person shows they are about to open it:
 * a mouse over a link to it, a finger or key pressed on one, or focus on it. An element
 * that opens a page without being a link names it in `data-prefetch`. `ahead` says what a
 * path needs loading; each one is started once. Nothing is loaded ahead while the browser
 * saves data. Returns the function that stops listening.
 */
export function prefetchOnIntent(
  ahead: (path: string) => Load | undefined,
): () => void {
  const started = new Set<Load>();
  const onIntent = (event: Event) => {
    if (
      event instanceof PointerEvent &&
      event.type === "pointerover" &&
      event.pointerType !== "mouse"
    )
      return;
    if (!(event.target instanceof Element)) return;
    const target = event.target.closest<HTMLElement>(
      "a[href], [data-prefetch]",
    );
    const href = target?.dataset.prefetch ?? target?.getAttribute("href");
    if (!href || savingData()) return;
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return;
    const load = ahead(url.pathname);
    if (!load || started.has(load)) return;
    started.add(load);
    load().catch(() => started.delete(load));
  };
  for (const type of INTENT)
    document.addEventListener(type, onIntent, { passive: true });
  return () => {
    for (const type of INTENT) document.removeEventListener(type, onIntent);
  };
}
