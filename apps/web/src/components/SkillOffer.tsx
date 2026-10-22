import { useState } from "react";
import { Puzzle } from "lucide-react";
import type { TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { Button } from "./ui/base";

/**
 * Under a result whose agent offers it: keeps how the task was done as a skill.
 * It sends `/skill` to the task; the agent drafts the skill and the person saves it.
 */
export function SkillOffer({
  task,
  onChange,
}: {
  task: TaskDetail;
  onChange: () => Promise<void>;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function keep() {
    setBusy(true);
    setError(undefined);
    try {
      await api.addInput(task.id, { text: "/skill" });
      await onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2">
      <Button
        size="sm"
        variant="ghost"
        icon={Puzzle}
        busy={busy}
        className="-ml-2.5"
        onClick={() => void keep()}
      >
        {t("result.keepSkill")}
      </Button>
      {error && (
        <p role="alert" className="mt-1 text-[13px] text-red">
          {error}
        </p>
      )}
    </div>
  );
}
