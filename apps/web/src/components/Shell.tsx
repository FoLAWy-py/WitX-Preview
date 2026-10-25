import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Outlet, useLocation, useNavigation } from "react-router";
import { Menu as MenuIcon } from "lucide-react";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/format";
import { useLayout } from "@/lib/prefs";
import { Button } from "./ui/base";
import { useDragToClose, useLayer, usePresence } from "./ui/dialog";
import { Sidebar } from "./Sidebar";

const ShellContext = createContext<{ openNav?: () => void }>({});

/** The navigation on phones: slides in from the left; a tap outside, Escape or a swipe
 * to the left closes it, and focus stays in it while it is open. */
function NavDrawer({
  open,
  onClose,
  label,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
}) {
  const { shown, leaving } = usePresence(open, 220);
  const drag = useDragToClose("left", onClose);
  const { reset } = drag;
  const panel = useLayer<HTMLElement>(open, onClose);
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);
  if (!shown) return null;
  return (
    <div
      className={cn(
        "fixed inset-0 z-[70] bg-black/40",
        leaving ? "anim-fade-out pointer-events-none" : "anim-fade",
      )}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <aside
        ref={panel}
        aria-label={label}
        data-drag-panel
        {...drag.handlers}
        style={drag.style("X")}
        className={cn(
          "h-full w-[86%] max-w-[320px] touch-pan-y border-r border-line bg-bg pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]",
          leaving ? "anim-drawer-out" : "anim-drawer",
        )}
      >
        <Sidebar onPick={onClose} />
      </aside>
    </div>
  );
}

/** How long a page may take to open before a bar at the top says it is coming. */
const PROGRESS_AFTER_MS = 300;

/** A thin bar across the top while the next page is still loading, once that is noticeable. */
function NavProgress() {
  const { t } = useI18n();
  const loading = useNavigation().state !== "idle";
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!loading) {
      setShown(false);
      return;
    }
    const timer = window.setTimeout(() => setShown(true), PROGRESS_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [loading]);
  if (!shown) return null;
  return (
    <div
      role="progressbar"
      aria-label={t("common.loading")}
      className="pointer-events-none fixed inset-x-0 top-0 z-[80] h-0.5 overflow-hidden"
    >
      <div className="anim-progress h-full w-1/3 bg-accent" />
    </div>
  );
}

/** Pages put their own header, body and right panel inside the shell. */
export function Shell() {
  const { t } = useI18n();
  const { layout } = useLayout();
  const location = useLocation();
  const [drawer, setDrawer] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("witx.sidebar") === "collapsed";
    } catch {
      return false;
    }
  });
  useEffect(() => setDrawer(false), [location.pathname]);
  const phoneContext = useMemo(() => ({ openNav: () => setDrawer(true) }), []);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  const wideContext = useMemo(() => ({}), []);
  const toggle = () => {
    setCollapsed((c) => {
      try {
        localStorage.setItem("witx.sidebar", c ? "open" : "collapsed");
      } catch {
        // not remembered
      }
      return !c;
    });
  };
  const narrow = layout !== "phone" && collapsed;

  if (layout === "phone") {
    return (
      // One screen tall, like the wide layout: each page scrolls its own body, so a
      // message box can stay at the bottom.
      <div className="flex h-dvh flex-col overflow-hidden">
        <NavProgress />
        <div className="flex min-h-0 flex-1">
          <ShellContext.Provider value={phoneContext}>
            <Outlet />
          </ShellContext.Provider>
        </div>
        <NavDrawer open={drawer} onClose={closeDrawer} label={t("nav.main")} />
      </div>
    );
  }

  return (
    <div className="flex h-dvh overflow-hidden">
      <NavProgress />
      <aside
        className={cn(
          "shrink-0 border-r border-line bg-bg transition-[width] duration-200 ease-out",
          narrow ? "w-[60px]" : "w-[264px]",
        )}
      >
        <Sidebar collapsed={narrow} onToggle={toggle} />
      </aside>
      <div className="flex min-w-0 flex-1">
        <ShellContext.Provider value={wideContext}>
          <Outlet />
        </ShellContext.Provider>
      </div>
    </div>
  );
}

/** The top bar of a page: on phones it opens the navigation drawer. */
export function PageBar({
  children,
  className,
}: {
  children?: ReactNode;
  className?: string;
}) {
  const { t } = useI18n();
  const { openNav } = useContext(ShellContext);
  // Nothing to show: only the navigation button, and there is no drawer here.
  if (!children && !openNav) return null;
  return (
    <header
      className={cn(
        "sticky top-0 z-30 flex min-h-13 items-center gap-2 border-b border-line bg-bg/90 px-3 py-2 backdrop-blur sm:px-4",
        className,
      )}
    >
      {openNav && (
        <Button
          variant="ghost"
          iconOnly
          icon={MenuIcon}
          aria-label={t("nav.open")}
          onClick={openNav}
        />
      )}
      {children}
    </header>
  );
}
