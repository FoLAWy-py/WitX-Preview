import type { Locale, Translate } from "@/i18n";

export function cn(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

export function relativeTime(iso: string, locale: Locale, now = Date.now()) {
  const diff = (new Date(iso).getTime() - now) / 1000;
  const rtf = new Intl.RelativeTimeFormat(locale, {
    numeric: "auto",
    style: "short",
  });
  const abs = Math.abs(diff);
  if (abs < 45) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day");
  return exactDate(iso, locale);
}

export const exactTime = (iso: string, locale: Locale) =>
  new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(iso));

export const exactDate = (iso: string, locale: Locale) =>
  new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" }).format(
    new Date(iso),
  );

export const clock = (iso: string, locale: Locale) =>
  new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(iso));

/** How long, compactly and to the second, like Codex's status line: "45s", "1m 05s",
 * "1h 02m 05s" ("45秒", "1分05秒", "1小时02分05秒"). */
export function duration(ms: number, t: Translate): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const two = (n: number) => String(n).padStart(2, "0");
  if (total < 60) return t("time.seconds", { n: total });
  const s = two(total % 60);
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return t("time.minutes", { m: minutes, s });
  return t("time.hours", {
    h: Math.floor(minutes / 60),
    m: two(minutes % 60),
    s,
  });
}

export function countdown(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function money(amount: string, currency: string, locale: Locale) {
  const value = Number(amount.replace(/[^\d.-]/g, ""));
  if (!Number.isFinite(value) || !/^[A-Z]{3}$/.test(currency)) {
    return `${amount} ${currency}`.trim();
  }
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
  }).format(value);
}

export const number = (n: number, locale: Locale) =>
  new Intl.NumberFormat(locale).format(n);

/** A moment in words, as precise as it needs to be: "13:16" today, "Oct 14, 13:16" this
 * year, with the year before that. */
export function moment(iso: string, locale: Locale, now = new Date()): string {
  const at = new Date(iso);
  const time: Intl.DateTimeFormatOptions = {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  };
  if (at.toDateString() === now.toDateString())
    return new Intl.DateTimeFormat(locale, time).format(at);
  return new Intl.DateTimeFormat(locale, {
    ...time,
    month: "short",
    day: "numeric",
    ...(at.getFullYear() !== now.getFullYear() && { year: "numeric" }),
  }).format(at);
}
