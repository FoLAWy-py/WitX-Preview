import { useCallback, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/format";
import { Ico } from "./base";
import {
  Placement,
  Popover,
  moveFocus,
  useFirstItemFocus,
  itemClass,
} from "./popover";

export interface SelectOption<T extends string> {
  value: T;
  label: string;
  description?: string;
  icon?: LucideIcon;
  /** When set, the option is shown but cannot be chosen, with this reason. */
  disabledReason?: string;
  group?: string;
  /** The riskiest choice: its name is red, in the list and on the trigger. */
  danger?: boolean;
}

/** Quieter text after the value on a trigger, e.g. a setting chosen in the footer;
 * its look can say how much it is (a higher effort reads stronger). */
export interface Suffix {
  text: string;
  icon?: LucideIcon;
  className?: string;
}

/** The look shared by every dropdown trigger: a pill with a chevron. */
export const triggerClass =
  "press inline-flex h-8 max-w-full items-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 text-[13px] font-medium text-fg hover:bg-sunken aria-expanded:bg-sunken disabled:opacity-45 pointer-coarse:min-h-11";

function OptionList<T extends string>({
  options,
  isChosen,
  onChoose,
  multi,
}: {
  options: SelectOption<T>[];
  isChosen: (value: T) => boolean;
  onChoose: (value: T) => void;
  multi?: boolean;
}) {
  let lastGroup: string | undefined;
  return options.map((o) => {
    const header =
      o.group && o.group !== lastGroup ? (
        <div
          key={`g-${o.group}`}
          className="px-2.5 pt-2 pb-1 font-mono text-[11.5px] font-medium text-faint"
        >
          {o.group}
        </div>
      ) : null;
    lastGroup = o.group;
    const chosen = isChosen(o.value);
    const disabled = !!o.disabledReason;
    return [
      header,
      <button
        key={o.value}
        type="button"
        role="option"
        data-item
        aria-selected={chosen}
        aria-disabled={disabled || undefined}
        onClick={() => !disabled && onChoose(o.value)}
        className={itemClass}
      >
        {multi ? (
          <span
            aria-hidden
            className={cn(
              "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border",
              chosen
                ? "border-accent bg-accent text-accent-fg"
                : "border-line-strong",
            )}
          >
            {chosen && <Check size={12} strokeWidth={2.5} />}
          </span>
        ) : (
          o.icon && (
            <Ico icon={o.icon} size={16} className="mt-0.5 text-muted" />
          )
        )}
        <span className="min-w-0 flex-1">
          <span className={cn("block truncate", o.danger && "text-red")}>
            {o.label}
          </span>
          {(o.disabledReason ?? o.description) && (
            <span className="block text-[12.5px] text-muted">
              {o.disabledReason ?? o.description}
            </span>
          )}
        </span>
        {!multi && chosen && (
          <Check
            size={16}
            strokeWidth={1.5}
            className="mt-0.5 shrink-0 text-accent"
          />
        )}
      </button>,
    ];
  });
}

/** Chooses one value from a list, shown as a pill that opens a dropdown. */
export function Select<T extends string>({
  value,
  options,
  onChange,
  label,
  disabled,
  icon,
  showLabel,
  placement = "bottom-start",
  minWidth = 260,
  suffix,
  footer,
  stayOpen,
  iconOnly,
}: {
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  label: string;
  disabled?: boolean;
  icon?: LucideIcon;
  /** Show the field's name before the value, e.g. "Model: gpt-6-luna". */
  showLabel?: boolean;
  placement?: Placement;
  minWidth?: number;
  /** Shown after the value on the trigger, quieter, e.g. a setting chosen in `footer`. */
  suffix?: Suffix;
  /** More controls below the list, e.g. how hard the chosen model thinks. */
  footer?: ReactNode;
  /** Keep the list open after a choice, for a footer that is often set right after. */
  stayOpen?: boolean;
  /** Show only the icon on the trigger (its name stays in the accessible label). */
  iconOnly?: boolean;
}) {
  const anchor = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const list = useFirstItemFocus(open);
  const close = useCallback(() => setOpen(false), []);
  const current = options.find((o) => o.value === value);
  const shownIcon = icon ?? current?.icon;
  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label}: ${current?.label ?? value}${suffix ? `, ${suffix.text}` : ""}`}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className={triggerClass}
      >
        {shownIcon && <Ico icon={shownIcon} size={15} className="text-muted" />}
        {showLabel && <span className="text-muted">{label}</span>}
        {!iconOnly && (
          <span className={cn("truncate", current?.danger && "text-red")}>
            {current?.label ?? value}
          </span>
        )}
        {suffix && (
          <span
            className={cn(
              "flex shrink-0 items-center gap-1",
              suffix.className ?? "text-muted",
            )}
          >
            <span className="text-muted">·</span>
            {suffix.icon && <Ico icon={suffix.icon} size={13} />}
            {suffix.text}
          </span>
        )}
        <ChevronDown
          size={14}
          strokeWidth={1.5}
          className="shrink-0 text-faint"
        />
      </button>
      <Popover
        anchor={anchor}
        open={open}
        onClose={close}
        placement={placement}
        title={label}
        minWidth={minWidth}
      >
        <div
          ref={list}
          role="listbox"
          aria-label={label}
          onKeyDown={(e) => {
            moveFocus(e, list.current);
            if (e.key === "Tab" && !footer) close();
          }}
        >
          <OptionList
            options={options}
            isChosen={(v) => v === value}
            onChoose={(v) => {
              if (!stayOpen) {
                close();
                anchor.current?.focus();
              }
              if (v !== value) onChange(v);
            }}
          />
        </div>
        {footer && (
          <>
            <div role="separator" className="my-1 h-px bg-line" />
            {footer}
          </>
        )}
      </Popover>
    </>
  );
}

/** Chooses several values; the list stays open while choosing. */
export function MultiSelect<T extends string>({
  values,
  options,
  onChange,
  label,
  summary,
  icon,
  empty,
  placement = "bottom-start",
  minWidth = 300,
}: {
  values: T[];
  options: SelectOption<T>[];
  onChange: (values: T[]) => void;
  label: string;
  summary: string;
  icon?: LucideIcon;
  empty?: string;
  placement?: Placement;
  minWidth?: number;
}) {
  const anchor = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const list = useFirstItemFocus(open);
  const close = useCallback(() => setOpen(false), []);
  return (
    <>
      <button
        ref={anchor}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label}: ${summary}`}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className={triggerClass}
      >
        {icon && <Ico icon={icon} size={15} className="text-muted" />}
        <span className="truncate">{summary}</span>
        <ChevronDown
          size={14}
          strokeWidth={1.5}
          className="shrink-0 text-faint"
        />
      </button>
      <Popover
        anchor={anchor}
        open={open}
        onClose={close}
        placement={placement}
        title={label}
        minWidth={minWidth}
      >
        <div
          ref={list}
          role="listbox"
          aria-multiselectable
          aria-label={label}
          onKeyDown={(e) => {
            moveFocus(e, list.current);
            if (e.key === "Tab") close();
          }}
        >
          {options.length === 0 && empty && (
            <p className="px-2.5 py-3 text-[13.5px] text-muted">{empty}</p>
          )}
          <OptionList
            multi
            options={options}
            isChosen={(v) => values.includes(v)}
            onChoose={(v) =>
              onChange(
                values.includes(v)
                  ? values.filter((x) => x !== v)
                  : [...values, v],
              )
            }
          />
        </div>
      </Popover>
    </>
  );
}
