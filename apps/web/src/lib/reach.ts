import { useEffect, useState } from "react";

/** How far before the end of the scrolled list the next page is asked for. */
const AHEAD_PX = 240;
/** How close to view, in sizes of the scrolled area, something starts to load; and how
 * far it must go before it is let go (apart, so that scrolling a little back and forth
 * does not load it again). */
const NEAR = "100%";
const FAR = "300%";

/** The closest ancestor that scrolls up and down now, or none (the page itself). A row
 * that only scrolls sideways is passed over: it still hides what is out of its sight,
 * and being near it says nothing when the row itself is far from view. */
function scroller(element: Element): Element | null {
  for (let at = element.parentElement; at; at = at.parentElement) {
    const { overflowY } = getComputedStyle(at);
    const scrolls = overflowY === "auto" || overflowY === "scroll";
    if (scrolls && at.scrollHeight > at.clientHeight) return at;
  }
  return null;
}

/**
 * Whether `element` is within reach of view: true once it scrolls within one size of the
 * scrolled area, false again once it is more than three away.
 */
export function useInReach(element: Element | null): boolean {
  const [inReach, setInReach] = useState(false);
  useEffect(() => {
    if (!element) return;
    const root = scroller(element);
    const near = new IntersectionObserver(
      (entries) => entries.some((e) => e.isIntersecting) && setInReach(true),
      { root, rootMargin: NEAR },
    );
    const far = new IntersectionObserver(
      (entries) => entries.some((e) => !e.isIntersecting) && setInReach(false),
      { root, rootMargin: FAR },
    );
    near.observe(element);
    far.observe(element);
    return () => {
      near.disconnect();
      far.disconnect();
    };
  }, [element]);
  return inReach;
}

/**
 * A marker for the end of a list (its `edge`: the bottom, or the top of a list that
 * grows upwards): `onNear` runs when it scrolls within reach of view, and again each
 * time `count` changes while it still is (a page too short to fill the view asks for
 * the next at once). Returns the ref for the marker.
 */
export function useNearEnd(
  onNear: () => void,
  enabled: boolean,
  count: number,
  edge: "top" | "bottom" = "bottom",
) {
  const [marker, setMarker] = useState<Element | null>(null);
  useEffect(() => {
    if (!marker || !enabled) return;
    const watch = new IntersectionObserver(
      (entries) => entries.some((e) => e.isIntersecting) && onNear(),
      {
        root: scroller(marker),
        rootMargin:
          edge === "top"
            ? `${AHEAD_PX}px 0px 0px 0px`
            : `0px 0px ${AHEAD_PX}px 0px`,
      },
    );
    watch.observe(marker);
    return () => watch.disconnect();
  }, [marker, enabled, count, onNear, edge]);
  return setMarker;
}
