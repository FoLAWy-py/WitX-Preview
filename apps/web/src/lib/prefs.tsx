import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

export type Theme = "light" | "dark" | "system";
const ThemeCtx = createContext<{
  theme: Theme;
  setTheme: (t: Theme) => void;
  resolved: "light" | "dark";
}>({
  theme: "system",
  setTheme: () => {},
  resolved: "light",
});

function useMedia(q: string) {
  const [m, setM] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const h = () => setM(mq.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, [q]);
  return m;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    try {
      const value = localStorage.getItem("witx.theme");
      return value === "light" || value === "dark" ? value : "system";
    } catch {
      return "system";
    }
  });
  const sysDark = useMedia("(prefers-color-scheme: dark)");
  const resolved = theme === "system" ? (sysDark ? "dark" : "light") : theme;
  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark");
  }, [resolved]);
  const setTheme = (t: Theme) => {
    setThemeState(t);
    try {
      localStorage.setItem("witx.theme", t);
    } catch {
      // not remembered, still applied
    }
  };
  return (
    <ThemeCtx.Provider value={{ theme, setTheme, resolved }}>
      {children}
    </ThemeCtx.Provider>
  );
}
export const useTheme = () => useContext(ThemeCtx);

export type Layout = "phone" | "tablet" | "desktop" | "large";
export interface Adaptive {
  layout: Layout;
  coarse: boolean;
  hover: boolean;
  landscapePhone: boolean;
  reducedMotion: boolean;
}

/** Detects viewport size and input modality; drives structurally different layouts. */
export function useAdaptive(): Adaptive {
  const [size, setSize] = useState({
    w: window.innerWidth,
    h: window.innerHeight,
  });
  useEffect(() => {
    const h = () => setSize({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", h);
    return () => window.removeEventListener("resize", h);
  }, []);
  const coarse = useMedia("(pointer: coarse)");
  const hover = useMedia("(hover: hover)");
  const reducedMotion = useMedia("(prefers-reduced-motion: reduce)");
  const landscapePhone = coarse && size.h < 500 && size.w > size.h;
  let layout: Layout =
    size.w < 640
      ? "phone"
      : size.w < 1024
        ? "tablet"
        : size.w < 1440
          ? "desktop"
          : "large";
  if (landscapePhone) layout = "phone";
  // Touch-only wide screens behave like tablets (no hover-only info, larger targets).
  if (coarse && !hover && (layout === "desktop" || layout === "large"))
    layout = "tablet";
  return { layout, coarse, hover, landscapePhone, reducedMotion };
}

const AdaptiveCtx = createContext<Adaptive | null>(null);
export function AdaptiveProvider({ children }: { children: ReactNode }) {
  const a = useAdaptive();
  return <AdaptiveCtx.Provider value={a}>{children}</AdaptiveCtx.Provider>;
}
export function useLayout(): Adaptive {
  const ctx = useContext(AdaptiveCtx);
  if (!ctx) throw new Error("useLayout outside AdaptiveProvider");
  return ctx;
}
