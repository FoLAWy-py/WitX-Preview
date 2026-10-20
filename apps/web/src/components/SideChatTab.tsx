import { useEffect, useState, type FormEvent } from "react";
import { ArrowUp, Trash2 } from "lucide-react";
import type { SideMessageView } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { useAsync } from "@/lib/useAsync";
import { Button } from "./ui/base";
import { Rich } from "./Rich";
import { Dialog } from "./ui/dialog";

/** A side chat: questions about the task and the answers. Nothing here reaches the
 * agent or the phones. */
export function ChatTab({
  thread,
  chats,
  onDeleted,
}: {
  thread: string;
  chats: SideChats;
  onDeleted: () => void;
}) {
  const { t } = useI18n();
  const [deleting, setDeleting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const messages = chats.messages.filter((m) => m.thread_id === thread);

  async function ask() {
    const question = text.trim();
    if (!question) return;
    setBusy(true);
    setError(undefined);
    try {
      await chats.ask(thread, question);
      setText("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="scroll-quiet min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {messages.length > 0 && (
          <div className="-mt-1 flex justify-end">
            <Button
              size="sm"
              variant="ghost"
              icon={Trash2}
              onClick={() => setDeleting(true)}
            >
              {t("side.delete")}
            </Button>
          </div>
        )}
        {messages.length === 0 && (
          <p className="rounded-xl border border-dashed border-line px-4 py-5 text-center text-[13px] text-muted">
            {t("side.empty")}
          </p>
        )}
        {messages.map((m) => (
          <SideExchange key={m.id} message={m} />
        ))}
      </div>
      <form
        className="border-t border-line p-3"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void ask();
        }}
      >
        <div className="flex items-end gap-2 rounded-xl border border-line-strong bg-surface p-1.5 focus-within:border-accent/60">
          <label htmlFor={`side-${thread}`} className="sr-only">
            {t("side.placeholder")}
          </label>
          <textarea
            id={`side-${thread}`}
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                void ask();
              }
            }}
            placeholder={t("side.placeholder")}
            className="block max-h-[30vh] min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-[14px] placeholder:text-faint focus:outline-none [field-sizing:content]"
          />
          <button
            type="submit"
            disabled={busy || !text.trim()}
            aria-label={t("side.send")}
            className="press flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-fg text-bg disabled:opacity-25 pointer-coarse:h-10 pointer-coarse:w-10"
          >
            <ArrowUp size={16} strokeWidth={2} aria-hidden />
          </button>
        </div>
        {error && (
          <p role="alert" className="mt-1.5 text-[12.5px] text-red">
            {error}
          </p>
        )}
      </form>
      {/* Closing a tab only puts a side chat away; deleting it is a decision. */}
      <Dialog
        open={deleting}
        onClose={() => setDeleting(false)}
        tone="red"
        title={t("side.delete.title")}
        footer={
          <>
            <Button data-autofocus onClick={() => setDeleting(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setDeleting(false);
                void chats.remove(thread).then(onDeleted);
              }}
            >
              {t("side.delete")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{t("side.delete.body")}</p>
      </Dialog>
    </div>
  );
}

function SideExchange({ message }: { message: SideMessageView }) {
  const { t } = useI18n();
  return (
    <div className="anim-slide space-y-2.5">
      <p className="ml-6 rounded-2xl rounded-tr-md bg-fill px-3.5 py-2 text-[14px] break-words whitespace-pre-wrap">
        {message.question}
      </p>
      {message.answer ? (
        <div className="text-[14px] leading-relaxed">
          <Rich text={message.answer} />
        </div>
      ) : message.error ? (
        <p className="text-[13px] text-red">
          {t("side.failed", { reason: message.error.replace(/_/g, " ") })}
        </p>
      ) : (
        <p className="flex items-center gap-2 text-[13px] text-muted">
          <span className="anim-pulse h-2 w-2 rounded-full bg-accent" />
          {t("side.thinking")}
        </p>
      )}
    </div>
  );
}

/** The task's side chats: their questions and answers. */
export function useSideChats(taskId: string) {
  const { api } = useAuth();
  const list = useAsync(() => api.sideChat(taskId), [api, taskId]);
  const { reload } = list;
  const messages = list.data ?? [];
  const waiting = messages.some((m) => m.answer === null && m.error === null);
  // The worker answers in a moment: look again until it has.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void reload(), 1500);
    return () => clearInterval(timer);
  }, [waiting, reload]);
  return {
    messages,
    /** Every side chat with a question in it, oldest first. */
    threads: [...new Set(messages.map((m) => m.thread_id))],
    ask: async (id: string, text: string) => {
      await api.askSideChat(taskId, { thread_id: id, text });
      await reload();
    },
    remove: async (id: string) => {
      await api.closeSideChat(taskId, id);
      await reload();
    },
  };
}
export type SideChats = ReturnType<typeof useSideChats>;
