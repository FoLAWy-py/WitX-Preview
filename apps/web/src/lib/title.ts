import { useEffect } from "react";

// The browser tab's title: "(2) Buy earbuds · WitX". The count is what waits for you
// (an approval, a question), so a person on another tab sees that they are needed.
let page = "";
let waiting = 0;

function apply() {
  const count = waiting > 0 ? `(${waiting}) ` : "";
  document.title = `${count}${page ? `${page} · ` : ""}WitX`;
}

/** Names the page in the tab's title while it is shown. */
export function usePageTitle(title: string | undefined) {
  useEffect(() => {
    page = title ?? "";
    apply();
    return () => {
      page = "";
      apply();
    };
  }, [title]);
}

/** How many tasks wait for the person, shown first in the tab's title. */
export function useWaitingCount(count: number) {
  useEffect(() => {
    waiting = count;
    apply();
  }, [count]);
}
