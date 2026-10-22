import { useEffect, type ComponentType, type ReactNode } from "react";
import {
  Link,
  Navigate,
  RouterProvider,
  createBrowserRouter,
  useLocation,
} from "react-router";
import { I18nProvider, useI18n } from "@/i18n";
import { AuthProvider, useAuth } from "@/lib/auth";
import { AdaptiveProvider, ThemeProvider } from "@/lib/prefs";
import { prefetchOnIntent, type Load } from "@/lib/prefetch";
import { WorkspaceProvider } from "@/lib/workspace";
import { loadMarkdown } from "@/components/Rich";
import { RouteError, StaleCode } from "@/components/RouteError";
import { PageBar, Shell } from "@/components/Shell";
import { EmptyState, Spinner } from "@/components/ui/base";
import SignIn from "@/pages/SignIn";
import Home from "@/pages/Home";
import Task from "@/pages/Task";

function RequireAuth({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const location = useLocation();
  if (me === undefined) return <Spinner className="min-h-dvh items-center" />;
  if (me === null)
    return (
      <Navigate to="/sign-in" replace state={{ from: location.pathname }} />
    );
  return <WorkspaceProvider>{children}</WorkspaceProvider>;
}

function NotFound() {
  const { t } = useI18n();
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <PageBar />
      <EmptyState
        title={t("notFound.title")}
        action={
          <Link to="/" className="text-[14px] font-medium text-accent">
            {t("notFound.back")}
          </Link>
        }
      />
    </div>
  );
}

/** The pages loaded when first opened, by path; the router keeps the current page until
 * the next one arrives. */
const PAGES = {
  "/devices": () => import("@/pages/Devices"),
  "/schedules": () => import("@/pages/Schedules"),
  "/customize": () => import("@/pages/Customize"),
  "/settings": () => import("@/pages/Settings"),
} satisfies Record<string, () => Promise<{ default: ComponentType }>>;

function page(path: keyof typeof PAGES) {
  return async () => {
    const module = await PAGES[path]().catch((error: unknown) => {
      throw new StaleCode(error);
    });
    return { Component: module.default };
  };
}

/** What to load ahead for a path the person is about to open. */
function ahead(path: string): Load | undefined {
  if (path in PAGES) return PAGES[path as keyof typeof PAGES];
  if (path.startsWith("/tasks/")) return loadMarkdown;
  return undefined;
}

const router = createBrowserRouter([
  { path: "/sign-in", element: <SignIn />, errorElement: <RouteError /> },
  {
    path: "/",
    element: (
      <RequireAuth>
        <Shell />
      </RequireAuth>
    ),
    // Opening a lazy page directly: the app's first paint, as while signing in is checked.
    hydrateFallbackElement: <Spinner className="min-h-dvh items-center" />,
    errorElement: <RouteError />,
    children: [
      {
        errorElement: <RouteError bar />,
        children: [
          { index: true, element: <Home /> },
          { path: "tasks/:taskId", element: <Task /> },
          { path: "devices", lazy: page("/devices") },
          { path: "schedules", lazy: page("/schedules") },
          { path: "customize", lazy: page("/customize") },
          { path: "settings", lazy: page("/settings") },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
]);

export default function App() {
  useEffect(() => prefetchOnIntent(ahead), []);
  return (
    <ThemeProvider>
      <I18nProvider>
        <AdaptiveProvider>
          <AuthProvider>
            <RouterProvider router={router} />
          </AuthProvider>
        </AdaptiveProvider>
      </I18nProvider>
    </ThemeProvider>
  );
}
