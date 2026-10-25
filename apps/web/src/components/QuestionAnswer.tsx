import { useState, type FormEvent } from "react";
import { ArrowUp, Check } from "lucide-react";
import type { TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/format";
import { Button } from "./ui/base";

/**
 * Answering the agent's open question, in its card: the answers it offers, one
 * per line with the recommended one first, each sent with one tap as if typed; and a
 * box for any other answer, sent with Enter. Either way the reply is the person's
 * message and the agent carries on. A sub-agent's question is answered to it alone
 * (``runId``, D57); the message box at the bottom writes to the main agent.
 */
export function QuestionAnswer({
  task,
  runId,
  options,
  recommended,
  onChange,
}: {
  task: TaskDetail;
  /** The sub-agent that asked; none for the main agent. */
  runId?: string;
  options: string[];
  recommended: number | null;
  onChange: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const order = options
    .map((option, index) => ({ option, index }))
    .sort(
      (a, b) =>
        Number(b.index === recommended) - Number(a.index === recommended),
    );

  async function answer(reply: string) {
    if (!reply.trim() || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      // Like a message to a paused task: it carries on.
      await api.addInput(task.id, {
        text: reply.trim(),
        resume: true,
        ...(runId && { run_id: runId }),
      });
      setText("");
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void answer(text);
  }

  return (
    <div className="mt-3 space-y-2 border-t border-amber/30 pt-3">
      {order.length > 0 && (
        <div
          role="group"
          aria-label={t("question.options")}
          className="flex flex-col gap-1.5"
        >
          {order.map(({ option, index }) => {
            const best = index === recommended;
            return (
              <button
                key={index}
                type="button"
                disabled={busy}
                onClick={() => void answer(option)}
                className={cn(
                  "press flex min-h-9 w-full items-center gap-2 rounded-lg border px-3 py-1.5 text-left text-[14px] disabled:opacity-50 pointer-coarse:min-h-11",
                  best
                    ? "border-accent bg-accent text-accent-fg hover:opacity-90"
                    : "border-line bg-surface text-fg hover:bg-hovered",
                )}
              >
                {best && (
                  <Check
                    size={15}
                    strokeWidth={2}
                    aria-hidden
                    className="shrink-0"
                  />
                )}
                <span className="min-w-0 flex-1 break-words">{option}</span>
                {best && (
                  <span className="shrink-0 text-[12px] opacity-80">
                    {t("question.recommended")}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
      <form onSubmit={submit} className="flex items-center gap-2">
        <label htmlFor={`answer-${task.id}`} className="sr-only">
          {t("question.answer")}
        </label>
        <input
          id={`answer-${task.id}`}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t(
            order.length ? "question.answerOther" : "question.answer",
          )}
          disabled={busy}
          autoComplete="off"
          className="h-9 min-w-0 flex-1 rounded-lg border border-line bg-surface px-3 text-[14px] placeholder:text-faint focus:border-amber focus:outline-none pointer-coarse:h-11"
        />
        {/* Not the recommended answer's color: sending is not a recommendation. */}
        <Button
          type="submit"
          size="sm"
          variant="secondary"
          icon={ArrowUp}
          busy={busy}
          disabled={!text.trim()}
          className="border-amber/50 text-amber"
        >
          {t("question.send")}
        </Button>
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-red">
          {error}
        </p>
      )}
    </div>
  );
}
