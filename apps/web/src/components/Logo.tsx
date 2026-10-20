import { Link } from "react-router";

export function Logo({ collapsed }: { collapsed?: boolean }) {
  return (
    <Link
      to="/"
      className="flex items-center gap-2.5 rounded-lg"
      aria-label="WitX"
    >
      <span
        aria-hidden
        className="relative flex h-7 w-7 items-center justify-center rounded-lg bg-fg"
      >
        <span className="h-4 w-2.5 rounded-[3px] border-[1.5px] border-bg" />
        <span className="absolute right-1 bottom-1 h-1.5 w-1.5 rounded-full bg-accent" />
      </span>
      {!collapsed && (
        <span className="text-[16px] font-semibold tracking-tight">WitX</span>
      )}
    </Link>
  );
}
