import { useState, type KeyboardEvent, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/format";
import { Ico, Kbd } from "./ui/base";

/**
 * A command offered when the message starts with "/", or a skill offered when the word
 * being typed starts with "$".
 */
export interface SlashCommand {
  name: string; // what is typed, e.g. "/goal" or "$my:open-wifi"
  hint: string;
  icon: LucideIcon;
  /** Put this in the box (e.g. "/goal ") for the person to finish; else `run`. */
  fill?: string;
  run?: () => void;
}

/** The "$…" word being typed at the end of the message, if any. */
export function namingAt(text: string): string | undefined {
  // As the server reads it: not after a Latin letter or digit, but right after Chinese.
  return /(?:^|[^A-Za-z0-9_$])(\$[^\s$]*)$/.exec(text)?.[1];
}

/**
 * The commands that start with what was typed ("/" alone: all of them), or the skills
 * whose name, or name without its plugin, starts with the "$…" word being typed.
 */
export function matchCommands(
  text: string,
  commands: SlashCommand[],
): SlashCommand[] {
  const naming = namingAt(text)?.toLowerCase();
  if (naming)
    return commands.filter(
      (c) =>
        c.name.startsWith("$") &&
        (c.name.startsWith(naming) ||
          `$${c.name.split(":")[1] ?? ""}`.startsWith(naming)),
    );
  if (!text.startsWith("/") || text.includes("\n")) return [];
  const typed = text.toLowerCase();
  return commands.filter(
    (c) =>
      c.name.startsWith("/") &&
      c.name.startsWith(typed) &&
      (c.fill ?? c.name) !== text,
  );
}

/**
 * The menu of commands above a message box, and the keys that drive it: arrows move,
 * Enter or Tab picks, Escape closes it for what is typed. `onKeyDown` returns true
 * when it used the key.
 */
export function useSlashMenu(
  text: string,
  commands: SlashCommand[],
  setText: (text: string) => void,
): {
  menu: ReactNode;
  onKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean;
} {
  const [index, setIndex] = useState(0);
  const [dismissed, setDismissed] = useState<string>();
  const items = matchCommands(text, commands);
  const open = items.length > 0 && dismissed !== text;
  const at = Math.min(index, items.length - 1);

  function pick(command: SlashCommand) {
    setIndex(0);
    const naming = namingAt(text);
    if (naming && command.name.startsWith("$")) {
      setText(`${text.slice(0, text.length - naming.length)}${command.name} `);
      return;
    }
    if (command.run) {
      setText("");
      command.run();
    } else setText(command.fill ?? `${command.name} `);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): boolean {
    if (!open || e.nativeEvent.isComposing) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setIndex((at + step + items.length) % items.length);
      return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      pick(items[at]);
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      setDismissed(text);
      return true;
    }
    return false;
  }

  const menu = open ? (
    <div
      role="listbox"
      aria-label={namingAt(text) ? "$" : "/"}
      className="anim-pop mb-1 overflow-hidden rounded-xl border border-line bg-sunken p-1"
    >
      {items.map((c, i) => (
        <button
          key={c.name}
          type="button"
          role="option"
          aria-selected={i === at}
          // Keep the focus in the box while choosing with the mouse.
          onMouseDown={(e) => e.preventDefault()}
          onMouseEnter={() => setIndex(i)}
          onClick={() => pick(c)}
          className={cn(
            "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-[13.5px] pointer-coarse:min-h-11",
            i === at && "bg-surface shadow-[0_0_0_1px_var(--line)]",
          )}
        >
          <Ico icon={c.icon} size={15} className="shrink-0 text-accent" />
          <span className="shrink-0 font-mono font-medium">{c.name}</span>
          <span className="min-w-0 flex-1 truncate text-muted">{c.hint}</span>
          {i === at && <Kbd className="max-sm:hidden">Tab</Kbd>}
        </button>
      ))}
    </div>
  ) : null;

  return { menu, onKeyDown };
}
