import { useState } from "react";
import { useSearchParams } from "react-router";
import { Puzzle, Search, Store, type LucideIcon } from "lucide-react";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/format";
import { usePageTitle } from "@/lib/title";
import { PageBar } from "@/components/Shell";
import { Plugins } from "@/components/Plugins";
import { Skills } from "@/components/Skills";
import { Ico } from "@/components/ui/base";

type Tab = "skills" | "plugins";
const TABS: [Tab, MessageKey, LucideIcon][] = [
  ["skills", "customize.tab.skills", Puzzle],
  ["plugins", "customize.tab.plugins", Store],
];

/**
 * What the agent knows how to do: the skills it can load, the person's own among
 * them, and the plugins they come from. The tab, and the skill being read,
 * are in the address, so a link opens them and Back returns from a skill.
 */
export default function Customize() {
  const { t } = useI18n();
  usePageTitle(t("customize.title"));
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "plugins" ? "plugins" : "skills";
  const choose = (next: Tab) =>
    setParams(next === "skills" ? {} : { tab: next });
  // One search for the open tab: skills, or plugins and what marketplaces offer.
  const [query, setQuery] = useState("");
  const searchLabel = t(tab === "skills" ? "skills.search" : "plugins.search");
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <PageBar>
        <h1 className="flex-1 text-[15px] font-semibold">
          {t("customize.title")}
        </h1>
      </PageBar>
      <main className="scroll-quiet flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1080px] space-y-5 px-4 py-6">
          <div className="flex flex-col items-center gap-3">
            <div
              role="tablist"
              aria-label={t("customize.title")}
              className="inline-flex gap-1 rounded-[10px] border border-line bg-sunken p-1"
            >
              {TABS.map(([id, label, icon]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  id={`customize-${id}`}
                  aria-selected={tab === id}
                  aria-controls="customize-panel"
                  onClick={() => choose(id)}
                  className={cn(
                    "flex h-8 items-center gap-1.5 rounded-md px-3 text-[13.5px] font-medium pointer-coarse:min-h-11",
                    tab === id
                      ? "bg-selected text-fg"
                      : "text-muted hover:bg-hovered hover:text-fg",
                  )}
                >
                  <Ico icon={icon} size={15} />
                  {t(label)}
                </button>
              ))}
            </div>
            {/* A skill being read takes the list's place on narrow screens: its search too. */}
            <label
              className={cn(
                "flex h-9 w-full max-w-[420px] cursor-text items-center gap-2 rounded-lg border border-line bg-surface px-3 text-muted focus-within:border-accent/60",
                tab === "skills" && params.get("skill") && "hidden md:flex",
              )}
            >
              <Ico icon={Search} size={15} />
              <span className="sr-only">{searchLabel}</span>
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={searchLabel}
                className="min-w-0 flex-1 bg-transparent text-[13.5px] text-fg placeholder:text-muted focus:outline-none"
              />
            </label>
          </div>
          <div
            role="tabpanel"
            id="customize-panel"
            aria-labelledby={`customize-${tab}`}
          >
            {tab === "skills" ? (
              <Skills
                query={query}
                open={params.get("skill") ?? undefined}
                onOpen={(name) => setParams(name ? { skill: name } : {})}
              />
            ) : (
              <Plugins
                query={query}
                onSkill={(name) => setParams({ skill: name })}
              />
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
