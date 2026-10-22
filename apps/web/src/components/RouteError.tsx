import { useEffect } from "react";
import { useRouteError } from "react-router";
import { useI18n } from "@/i18n";
import { PageBar } from "@/components/Shell";
import { Button, EmptyState } from "@/components/ui/base";

/** A page's code did not load: usually a newer version was deployed and the old files are gone. */
export class StaleCode extends Error {
  constructor(readonly reason: unknown) {
    super("A page's code did not load");
  }
}

const RELOADED = "witx.reloadedForCode";
const AGAIN_AFTER_MS = 10_000;

/** Whether the app was reloaded for missing code moments ago, so doing it again would loop. */
function reloadedJustNow(): boolean {
  try {
    const at = Number(sessionStorage.getItem(RELOADED));
    return Date.now() - at < AGAIN_AFTER_MS;
  } catch {
    return false;
  }
}

function reload() {
  try {
    sessionStorage.setItem(RELOADED, String(Date.now()));
  } catch {
    // Without storage the guard is gone; the reload still fetches the new version.
  }
  window.location.reload();
}

/**
 * What a route shows when it fails. Code that did not load is fetched again with one
 * reload, which also brings the newest version; past that, or for any other error, the
 * page says it did not open and offers a reload.
 */
export function RouteError({ bar = false }: { bar?: boolean }) {
  const { t } = useI18n();
  const error = useRouteError();
  const retry = error instanceof StaleCode && !reloadedJustNow();
  useEffect(() => {
    if (retry) reload();
  }, [retry]);
  if (retry) return null;
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      {bar && <PageBar />}
      <EmptyState
        title={t("routeError.title")}
        action={
          <Button variant="primary" onClick={reload}>
            {t("routeError.reload")}
          </Button>
        }
      />
    </div>
  );
}
