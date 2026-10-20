import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import en, { type MessageKey, type Plural } from "./en";
import zh from "./zh";

export type Locale = "en" | "zh-CN";
export const LOCALES: { id: Locale; label: string }[] = [
  { id: "en", label: "English" },
  { id: "zh-CN", label: "简体中文" },
];
const catalogs = { en, "zh-CN": zh } as const;
const STORAGE = "witx.locale";

type Vars = Record<string, string | number>;
export type Translate = (key: MessageKey, vars?: Vars) => string;

interface I18n {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: Translate;
}

const Ctx = createContext<I18n | null>(null);

function saved(): Locale {
  try {
    const value = localStorage.getItem(STORAGE);
    return value === "zh-CN" ? "zh-CN" : "en";
  } catch {
    return "en";
  }
}

/** English unless the person chose Simplified Chinese; the choice is remembered. */
export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(saved);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      localStorage.setItem(STORAGE, next);
    } catch {
      // not remembered, still switched
    }
  }, []);

  const t = useCallback<Translate>(
    (key, vars) => {
      const catalog = catalogs[locale] as Record<string, string | Plural>;
      let message =
        catalog[key] ?? (en as Record<string, string | Plural>)[key] ?? key;
      if (typeof message !== "string") {
        const n = Number(vars?.count ?? 0);
        message =
          new Intl.PluralRules(locale).select(n) === "one"
            ? message.one
            : message.other;
      }
      if (vars) {
        message = message.replace(/\{(\w+)\}/g, (_, name: string) => {
          const value = vars[name];
          if (value === undefined) return `{${name}}`;
          return typeof value === "number"
            ? value.toLocaleString(locale)
            : value;
        });
      }
      return message;
    },
    [locale],
  );

  const value = useMemo(
    () => ({ locale, setLocale, t }),
    [locale, setLocale, t],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18n {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useI18n outside I18nProvider");
  return ctx;
}

export type { MessageKey };
