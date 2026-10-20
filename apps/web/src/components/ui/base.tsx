import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { AlertCircle, Loader, RotateCw, type LucideIcon } from "lucide-react";
import { useI18n } from "@/i18n";
import { cn, exactTime, relativeTime } from "@/lib/format";

export type Tone =
  | "neutral"
  | "accent"
  | "amber"
  | "red"
  | "green"
  | "violet"
  | "slate";

const toneClass: Record<Tone, string> = {
  neutral: "bg-sunken text-muted border-line",
  accent: "bg-accent-soft text-accent border-accent/25",
  amber: "bg-amber-soft text-amber border-amber/30",
  red: "bg-red-soft text-red border-red/30",
  green: "bg-green-soft text-green border-green/30",
  violet: "bg-violet-soft text-violet border-violet/30",
  slate: "bg-slate-soft text-slate border-slate/25",
};

export const Ico = ({
  icon: Icon,
  size = 16,
  className,
}: {
  icon: LucideIcon;
  size?: number;
  className?: string;
}) => (
  <Icon
    size={size}
    strokeWidth={1.5}
    aria-hidden
    className={cn("shrink-0", className)}
  />
);

type Variant = "primary" | "secondary" | "ghost" | "danger" | "danger-soft";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md" | "lg";
  icon?: LucideIcon;
  iconOnly?: boolean;
  busy?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      size = "md",
      icon,
      iconOnly,
      busy,
      className,
      children,
      disabled,
      type = "button",
      ...rest
    },
    ref,
  ) {
    const variants: Record<Variant, string> = {
      primary: "bg-accent text-accent-fg hover:opacity-90 border-transparent",
      secondary: "bg-surface text-fg border-line-strong hover:bg-sunken",
      ghost:
        "bg-transparent text-muted border-transparent hover:bg-sunken hover:text-fg",
      danger:
        "bg-red text-white dark:text-[#1a0b0b] border-transparent hover:opacity-90",
      "danger-soft": "bg-surface text-red border-red/40 hover:bg-red-soft",
    };
    const sizes = {
      sm: "h-8 px-2.5 text-[13px] gap-1.5",
      md: "h-9 px-3.5 text-[14px] gap-2",
      lg: "h-11 px-5 text-[15px] gap-2",
    }[size];
    return (
      <button
        ref={ref}
        type={type}
        disabled={disabled || busy}
        aria-busy={busy || undefined}
        className={cn(
          "press inline-flex items-center justify-center border font-medium whitespace-nowrap disabled:pointer-events-none disabled:opacity-45 pointer-coarse:min-h-11",
          // A button that is only an icon is a circle, everywhere.
          iconOnly ? "rounded-full" : "rounded-lg",
          variants[variant],
          sizes,
          iconOnly &&
            (size === "sm" ? "w-8 px-0" : "w-9 px-0") +
              " pointer-coarse:min-w-11",
          className,
        )}
        {...rest}
      >
        {busy ? (
          <Ico
            icon={Loader}
            size={size === "lg" ? 18 : 16}
            className="animate-spin"
          />
        ) : (
          icon && <Ico icon={icon} size={size === "lg" ? 18 : 16} />
        )}
        {children}
      </button>
    );
  },
);

export const Kbd = ({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) => (
  <kbd
    className={cn(
      "inline-flex h-5 min-w-5 items-center justify-center rounded border border-current/25 px-1 font-mono text-[11px] opacity-70",
      className,
    )}
  >
    {children}
  </kbd>
);

export function Chip({
  tone = "neutral",
  icon,
  children,
  pulse,
  className,
  title,
}: {
  tone?: Tone;
  icon?: LucideIcon;
  children: ReactNode;
  pulse?: boolean;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-[12.5px] font-medium whitespace-nowrap",
        toneClass[tone],
        className,
      )}
    >
      {icon && (
        <Ico
          icon={icon}
          size={13}
          className={pulse ? "anim-pulse" : undefined}
        />
      )}
      {children}
    </span>
  );
}

export const Mono = ({
  children,
  className,
  title,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
}) => (
  <span title={title} className={cn("font-mono text-[12.5px] tnum", className)}>
    {children}
  </span>
);

export function Card({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cn("rounded-xl border border-line bg-surface", className)}
    >
      {children}
    </section>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  className,
}: {
  value: T;
  options: {
    value: T;
    label: string;
    icon?: LucideIcon;
    disabled?: boolean;
    title?: string;
    /** The option's own text color, chosen or not (e.g. a stronger effort). */
    tone?: string;
  }[];
  onChange: (v: T) => void;
  label: string;
  className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const dir = e.key === "ArrowRight" ? 1 : -1;
    for (let k = 1; k <= options.length; k++) {
      const j = (i + dir * k + options.length) % options.length;
      if (!options[j].disabled) {
        refs.current[j]?.focus();
        onChange(options[j].value);
        break;
      }
    }
  };
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        "inline-flex flex-wrap gap-1 rounded-[10px] border border-line bg-sunken p-1",
        className,
      )}
    >
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            disabled={o.disabled}
            title={o.title}
            onKeyDown={(e) => onKey(e, i)}
            onClick={() => onChange(o.value)}
            className={cn(
              "press inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-[13.5px] font-medium whitespace-nowrap disabled:opacity-45 pointer-coarse:min-h-11",
              on && "bg-selected",
              // A toned option keeps its color; the chosen one is also bold and the
              // others quieter, so the choice still stands out.
              o.tone
                ? cn(
                    o.tone,
                    on ? "font-semibold" : "opacity-60 hover:opacity-100",
                  )
                : on
                  ? "text-fg"
                  : "text-muted hover:text-fg",
            )}
          >
            {o.icon && <Ico icon={o.icon} size={15} />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-6 w-10 shrink-0 items-center rounded-full border transition-colors duration-150 disabled:opacity-45",
        checked ? "border-accent bg-accent" : "border-line-strong bg-sunken",
      )}
    >
      <span
        className={cn(
          "absolute h-4.5 w-4.5 rounded-full bg-white shadow-sm transition-transform duration-150 ease-out",
          checked ? "translate-x-[18px]" : "translate-x-[3px]",
        )}
      />
    </button>
  );
}

export const Skeleton = ({ className }: { className?: string }) => (
  <div aria-hidden className={cn("skeleton", className)} />
);

export function Spinner({ className }: { className?: string }) {
  const { t } = useI18n();
  return (
    <div
      role="status"
      aria-label={t("common.loading")}
      className={cn("flex justify-center py-10 text-faint", className)}
    >
      <Ico icon={Loader} size={20} className="animate-spin" />
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  const { t } = useI18n();
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 rounded-xl border border-red/30 bg-red-soft px-4 py-3 text-[14px] text-red"
    >
      <Ico icon={AlertCircle} />
      <span className="flex-1 text-fg">{message}</span>
      {onRetry && (
        <Button size="sm" icon={RotateCw} onClick={onRetry}>
          {t("common.retry")}
        </Button>
      )}
    </div>
  );
}

export function EmptyState({
  art,
  title,
  body,
  action,
}: {
  art?: ReactNode;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-12 text-center">
      {art}
      <h3 className="mt-5 text-[15px] font-semibold">{title}</h3>
      {body && (
        <div className="mt-1.5 max-w-sm text-[14px] text-muted">{body}</div>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

/** Relative time; the exact time on hover. */
export function RelTime({
  iso,
  className,
}: {
  iso: string;
  className?: string;
}) {
  const { locale } = useI18n();
  const [, tick] = useState(0);
  useEffect(() => {
    const h = setInterval(() => tick((x) => x + 1), 30000);
    return () => clearInterval(h);
  }, []);
  return (
    <time
      dateTime={iso}
      title={exactTime(iso, locale)}
      className={cn("tnum", className)}
    >
      {relativeTime(iso, locale)}
    </time>
  );
}

export function Field({
  label,
  hint,
  children,
  htmlFor,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  htmlFor: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-[13px] font-medium text-fg">
        {label}
      </label>
      {children}
      {hint && <p className="text-[12.5px] text-muted">{hint}</p>}
    </div>
  );
}

export const inputClass =
  "w-full rounded-lg border border-line-strong bg-surface px-3 py-2 text-[15px] text-fg placeholder:text-faint focus:border-accent focus:outline-none pointer-coarse:min-h-11";

/** Platform glyphs: device silhouettes in the Lucide 1.5px style. */
export function PlatformIcon({
  platform,
  size = 16,
  className,
}: {
  platform: "ios" | "android" | "fake";
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      aria-hidden
      className={cn("shrink-0", className)}
    >
      <rect
        x="6"
        y="2.5"
        width="12"
        height="19"
        rx={platform === "ios" ? 3.2 : 2}
      />
      {platform === "ios" ? (
        <path d="M10.5 5h3" />
      ) : (
        <circle cx="12" cy="5.2" r="0.6" fill="currentColor" />
      )}
    </svg>
  );
}

/* ---------------------------------------------------------------- dropdowns */
