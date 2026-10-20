import { useCallback, useState } from "react";

const KEY = "witx.panelWidth";
export const PANEL_MIN = 300;
const PANEL_MAX = 720;

/** The side panel's width kept between bounds: at least 300 px, at most 720 px and
 * never more than half the window. */
export function clampPanel(width: number, windowWidth: number): number {
  const max = Math.max(
    PANEL_MIN,
    Math.min(PANEL_MAX, Math.floor(windowWidth / 2)),
  );
  return Math.round(Math.min(max, Math.max(PANEL_MIN, width)));
}

function stored(): number | undefined {
  try {
    const value = Number(localStorage.getItem(KEY));
    return Number.isFinite(value) && value > 0 ? value : undefined;
  } catch {
    return undefined;
  }
}

/** The side panel's width as the person last left it (in this browser), or the
 * layout's default. `set` keeps a new one; `reset` goes back to the default. */
export function usePanelWidth(fallback: number) {
  const [chosen, setChosen] = useState<number | undefined>(stored);
  const width = clampPanel(chosen ?? fallback, window.innerWidth);
  const set = useCallback((next: number) => {
    const value = clampPanel(next, window.innerWidth);
    setChosen(value);
    try {
      localStorage.setItem(KEY, String(value));
    } catch {
      // private window: kept for this page only
    }
  }, []);
  const reset = useCallback(() => {
    setChosen(undefined);
    try {
      localStorage.removeItem(KEY);
    } catch {
      // nothing kept
    }
  }, []);
  return { width, set, reset };
}
