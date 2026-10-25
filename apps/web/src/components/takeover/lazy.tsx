import { lazy, Suspense, type ComponentProps } from "react";
import { StaleCode } from "@/components/RouteError";
import type { Takeover as Loaded } from "../Takeover";

let loading: Promise<{ default: typeof Loaded }> | undefined;

/** Loads the takeover's code once; called as a takeover is asked for, so the two overlap. */
export function loadTakeover() {
  loading ??= import("../Takeover").then(
    (module) => ({ default: module.Takeover }),
    (error: unknown) => {
      loading = undefined;
      throw new StaleCode(error);
    },
  );
  return loading;
}

const Lazy = lazy(loadTakeover);

/** The takeover, its code fetched the first time one starts; nothing shows until then. */
export function Takeover(props: ComponentProps<typeof Loaded>) {
  return (
    <Suspense fallback={null}>
      <Lazy {...props} />
    </Suspense>
  );
}
