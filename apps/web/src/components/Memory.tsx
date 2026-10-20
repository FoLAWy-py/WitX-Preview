import { useState } from "react";
import { Bookmark } from "lucide-react";
import type { MemoryView } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { Ico } from "./ui/base";

/** What an entry is about: a preference, a correction, or an app. */
export function memoryKind(
  memory: MemoryView,
  t: ReturnType<typeof useI18n>["t"],
): string {
  return memory.kind === "app"
    ? t("memory.kind.app", { app: memory.app ?? "" })
    : t(
        memory.kind === "feedback"
          ? "memory.kind.feedback"
          : "memory.kind.user",
      );
}

/**
 * Something the agent kept for the person's future tasks on its own (D59, after Claude
 * Code's auto memory): one line in the conversation, undone with one click.
 */
export function MemoryCard({
  memory,
  onChange,
}: {
  memory: MemoryView;
  onChange: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function undo() {
    setBusy(true);
    setError(undefined);
    try {
      await api.deleteMemory(memory.id);
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="note"
      aria-label={t("memory.saved")}
      className="flex items-start gap-2 pl-9 text-[13px] leading-relaxed text-muted"
    >
      <Ico icon={Bookmark} size={13} className="mt-1 shrink-0" />
      <p className="min-w-0 flex-1 break-words">
        <span className="font-medium">
          {t("memory.savedAs", { kind: memoryKind(memory, t) })}
        </span>{" "}
        {memory.text}
      </p>
      <button
        type="button"
        disabled={busy}
        onClick={() => void undo()}
        className="shrink-0 cursor-pointer text-muted underline-offset-2 hover:text-fg hover:underline disabled:opacity-50"
      >
        {t("memory.undo")}
      </button>
      {error && (
        <p role="alert" className="text-red">
          {error}
        </p>
      )}
    </div>
  );
}
