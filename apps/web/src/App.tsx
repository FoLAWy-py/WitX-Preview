import { type ReactNode } from "react";
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
import { WorkspaceProvider } from "@/lib/workspace";
import { PageBar, Shell } from "@/components/Shell";
import { EmptyState, Spinner } from "@/components/ui/base";
import SignIn from "@/pages/SignIn";
import Home from "@/pages/Home";
import Task from "@/pages/Task";
import Devices from "@/pages/Devices";
import Customize from "@/pages/Customize";
import Schedules from "@/pages/Schedules";
import Settings from "@/pages/Settings";

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

const router = createBrowserRouter([
  { path: "/sign-in", element: <SignIn /> },
  {
    path: "/",
    element: (
      <RequireAuth>
        <Shell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Home /> },
      { path: "tasks/:taskId", element: <Task /> },
      { path: "devices", element: <Devices /> },
      { path: "schedules", element: <Schedules /> },
      { path: "customize", element: <Customize /> },
      { path: "settings", element: <Settings /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

export default function App() {
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
