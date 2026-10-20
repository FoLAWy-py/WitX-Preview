import type { DeviceView, RunView, TaskDetail } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { agentName, runLabel } from "@/lib/agents";
import type { TimelineItem } from "@/lib/timeline";
import { Conversation } from "./Conversation";
import { PlatformIcon } from "./ui/base";

/** The items of the conversation that are this agent's: what it said and asked, the
 * answers and messages it got, its steps, and its result. */
export function agentItems(
  items: TimelineItem[],
  run: RunView,
): TimelineItem[] {
  const own = items.filter(
    (i) =>
      (i.kind === "action" && i.action.run_id === run.id) ||
      ((i.kind === "message" || i.kind === "question") && i.runId === run.id) ||
      (i.kind === "relay" && i.toRunId === run.id) ||
      (i.kind === "input" && i.input.run_id === run.id),
  );
  if (run.result && run.parent_run_id)
    own.push({
      kind: "result",
      at: run.result.completed_at,
      key: `result-${run.id}`,
      result: run.result,
      latest: false,
      failed: run.status === "failed",
    });
  return own;
}

/** A sub-agent in the side panel: who it is, on which phone, how it is doing, its
 * sub-goal, and its part of the conversation. */
export function AgentTab({
  task,
  run,
  items,
  devices,
  onChanged,
  onTakeOver,
}: {
  task: TaskDetail;
  run: RunView;
  items: TimelineItem[];
  devices: DeviceView[];
  onChanged: () => Promise<void>;
  onTakeOver: (deviceId: string) => void;
}) {
  const { t } = useI18n();
  const phone = devices.find((d) => d.id === run.device_ids[0]);
  const own = agentItems(items, run);
  return (
    <div className="scroll-quiet h-full overflow-y-auto">
      <div className="space-y-4 p-4">
        <header className="space-y-1.5">
          <h2 className="text-[15px] font-semibold">
            {agentName(run, task.runs, devices, t)}
          </h2>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12.5px] text-muted">
            {phone && (
              <span className="flex items-center gap-1">
                <PlatformIcon platform={phone.platform} size={13} />
                {phone.name}
              </span>
            )}
            <span>{runLabel(run, devices, t)}</span>
          </p>
          <p className="rounded-lg bg-sunken px-3 py-2 text-[13px]">
            <span className="text-faint">{t("agents.goal")}: </span>
            {run.goal}
          </p>
        </header>
        {own.length ? (
          <Conversation
            task={task}
            items={own}
            devices={devices}
            onChange={onChanged}
            onTakeOver={onTakeOver}
            scope={run}
          />
        ) : (
          <p className="text-[13px] text-muted">{t("agents.nothing")}</p>
        )}
      </div>
    </div>
  );
}
