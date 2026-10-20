import { useState } from "react";
import { ClipboardList, CornerDownLeft, Play } from "lucide-react";
import type { TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { Button, Ico } from "./ui/base";
import { Rich } from "./Rich";

/**
 * The plan the agent presented in plan mode. The newest one, while the task is
 * still in plan mode, can be approved (a `/plan approve` message: the agent may act from
 * then on) or answered with what to change, in the message box.
 */
export function PlanCard({
  task,
  text,
  open,
  onChange,
}: {
  task: TaskDetail;
  text: string;
  open: boolean;
  onChange: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function approve() {
    setBusy(true);
    setError(undefined);
    try {
      await api.addInput(task.id, { text: "/plan approve" });
      // Like a message to a paused task: it carries on.
      if (task.status === "paused") await api.resume(task.id);
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      aria-label={t("plan.title")}
      className="rounded-xl border border-accent/40 bg-surface px-4 py-3"
    >
      <div className="mb-1 flex items-center gap-1.5 text-[12.5px] font-medium text-accent">
        <Ico icon={ClipboardList} size={14} />
        {t("plan.title")}
      </div>
      <div className="text-[15px] leading-relaxed">
        <Rich text={text} />
      </div>
      {open && (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
          <Button
            size="sm"
            variant="primary"
            icon={Play}
            busy={busy}
            disabled={busy}
            onClick={() => void approve()}
          >
            {t("plan.approve")}
          </Button>
          <Button
            size="sm"
            icon={CornerDownLeft}
            disabled={busy}
            onClick={() => document.getElementById("note")?.focus()}
          >
            {t("plan.change")}
          </Button>
          <span className="text-[12.5px] text-muted">{t("plan.hint")}</span>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </section>
  );
}
