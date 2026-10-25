import { useState } from "react";
import { Check, Copy, Pencil, RotateCcw } from "lucide-react";
import type { TaskDetail, TaskInputView } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/format";
import { Ico } from "./ui/base";

const button =
  "flex h-7 w-7 items-center justify-center rounded-full text-faint hover:bg-hovered hover:text-fg disabled:opacity-40 pointer-coarse:h-9 pointer-coarse:w-9";

/**
 * Under one of the person's messages: copy its text, edit it (the text goes into the
 * message box, to be sent as a new message: what happened on the phones stays done),
 * or send it again. Shown on hover or focus; always on touch.
 */
export function MessageActions({
  task,
  input,
  onEdit,
  onChange,
}: {
  task: TaskDetail;
  input: TaskInputView;
  onEdit?: (text: string) => void;
  onChange: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  // A constraint is not a message to send again; a stopping task takes none.
  const resend = input.kind !== "constraint" && task.status !== "stopping";
  // A command (/plan approve, /goal …) acts on the task as it is now: sending an old
  // one again with one tap could approve a different plan or reset the goal.
  const command = input.text.trimStart().startsWith("/");

  async function copy() {
    try {
      await navigator.clipboard.writeText(input.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError(t("message.copyFailed"));
    }
  }

  async function retry() {
    setBusy(true);
    setError(undefined);
    try {
      await api.addInput(task.id, { text: input.text, resume: true });
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end">
      <div
        className={cn(
          "mt-0.5 flex items-center gap-0.5 opacity-0 transition-opacity group-hover/msg:opacity-100 focus-within:opacity-100 pointer-coarse:opacity-100",
          (copied || busy || error) && "opacity-100",
        )}
      >
        <button
          type="button"
          aria-label={t(copied ? "message.copied" : "message.copy")}
          title={t(copied ? "message.copied" : "message.copy")}
          onClick={() => void copy()}
          className={button}
        >
          <Ico icon={copied ? Check : Copy} size={14} />
        </button>
        {resend && onEdit && (
          <button
            type="button"
            aria-label={t("message.edit")}
            title={t("message.edit")}
            onClick={() => onEdit(input.text)}
            className={button}
          >
            <Ico icon={Pencil} size={14} />
          </button>
        )}
        {resend && !command && (
          <button
            type="button"
            aria-label={t("message.retry")}
            title={t("message.retry")}
            disabled={busy}
            onClick={() => void retry()}
            className={button}
          >
            <Ico icon={RotateCcw} size={14} />
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-[12.5px] text-red">
          {error}
        </p>
      )}
    </div>
  );
}
