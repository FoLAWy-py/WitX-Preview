import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ChevronRight } from "lucide-react";
import { useI18n, type MessageKey } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { useWorkspace } from "@/lib/workspace";
import { NewTaskComposer } from "@/components/Composer";
import { PageBar } from "@/components/Shell";
import { RelTime } from "@/components/ui/base";

const SUGGESTIONS: MessageKey[] = [
  "home.suggest.version",
  "home.suggest.reminder",
  "home.suggest.compare",
];

/** A new task, like a new conversation: the goal first, phones and permissions below. */
export default function Home() {
  const { t } = useI18n();
  const { me } = useAuth();
  const { attention } = useWorkspace();
  const [seed, setSeed] = useState("");
  const [params] = useSearchParams();
  const waiting = attention.data ?? [];
  const hour = new Date().getHours();
  const greeting =
    hour < 12 ? "home.morning" : hour < 18 ? "home.afternoon" : "home.evening";
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <PageBar />
      {/* Like a conversation: what to say about sits in the middle, the box to say it
          in at the bottom, where the reply box of a task is too. */}
      <main className="scroll-quiet flex min-h-0 flex-1 flex-col overflow-y-auto px-4">
        <div className="mx-auto my-auto w-full max-w-[720px] py-8">
          <h1 className="mb-1 text-center text-[26px] font-semibold tracking-tight sm:text-[28px]">
            {t(greeting, { name: me?.display_name ?? "" })}
          </h1>
          <p className="text-center text-[15px] text-muted">
            {t("home.subtitle")}
          </p>
          {/* Ideas to start with, while nothing waits for you; then what waits comes first. */}
          {attention.data && waiting.length === 0 && (
            <div className="mt-8 flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((key) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setSeed(t(key))}
                  className="press rounded-full border border-line bg-surface px-3.5 py-1.5 text-[13px] text-muted hover:bg-sunken hover:text-fg pointer-coarse:min-h-11"
                >
                  {t(key)}
                </button>
              ))}
            </div>
          )}
          {waiting.length > 0 && (
            <section className="mt-8">
              <h2 className="mb-2 flex items-center gap-2 text-[12.5px] font-medium text-muted">
                <span
                  aria-hidden
                  className="h-1.5 w-1.5 rounded-full bg-amber"
                />
                {t("home.waiting")}
                <span className="tnum text-faint">{waiting.length}</span>
              </h2>
              {/* One list, not a stack of alarms: the dot says it waits, the row says what. */}
              <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface">
                {waiting.map((task) => (
                  <li key={task.id}>
                    <Link
                      to={`/tasks/${task.id}`}
                      title={task.request}
                      className="group flex items-center gap-3 px-4 py-3 text-[14px] hover:bg-sunken pointer-coarse:min-h-12"
                    >
                      <span className="min-w-0 flex-1 truncate">
                        {task.title ?? task.request}
                      </span>
                      <RelTime
                        iso={task.updated_at}
                        className="shrink-0 text-[12.5px] text-faint"
                      />
                      <ChevronRight
                        size={16}
                        aria-hidden
                        className="shrink-0 text-faint transition-transform group-hover:translate-x-0.5"
                      />
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </main>
      <div className="mx-auto w-full max-w-[760px] px-4 pt-2 pb-[max(1.5rem,calc(env(safe-area-inset-bottom)+0.5rem))]">
        <NewTaskComposer
          key={`${seed}|${params.get("schedule") ?? ""}`}
          initialGoal={seed}
          openSchedule={params.get("schedule") === "1"}
        />
      </div>
    </div>
  );
}
