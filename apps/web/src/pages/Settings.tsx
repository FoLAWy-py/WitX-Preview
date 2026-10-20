import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import {
  AlertTriangle,
  ArrowRight,
  Bookmark,
  Cloud,
  Cpu,
  FileText,
  Gauge,
  Images,
  Puzzle,
  Search,
  ShieldAlert,
  Shuffle,
  type LucideIcon,
} from "lucide-react";
import type { TenantSettings } from "@/api/types.gen";
import { usePageTitle } from "@/lib/title";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/format";
import { useAuth } from "@/lib/auth";
import { useAsync } from "@/lib/useAsync";
import { useWorkspace } from "@/lib/workspace";
import { PageBar } from "@/components/Shell";
import {
  Button,
  Card,
  Chip,
  Ico,
  Segmented,
  Toggle,
  inputClass,
} from "@/components/ui/base";
import { Dialog } from "@/components/ui/dialog";
import { memoryKind } from "@/components/Memory";
import { AppNotesToSkill } from "@/components/Skills";

/**
 * One tenant-wide setting: what it does, its switch (owners only), and an optional
 * warning to accept before it is turned on.
 */
function Setting({
  title,
  help,
  icon,
  on,
  name,
  beta,
  warning,
}: {
  title: string;
  help: string;
  icon: typeof Gauge;
  on: boolean;
  name: keyof TenantSettings;
  beta?: boolean;
  warning?: {
    title: string;
    body: ReactNode;
    confirm: string;
  };
}) {
  const { t } = useI18n();
  const { api, me, refresh } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [asking, setAsking] = useState(false);
  const owner = me?.role === "owner";

  async function set(value: boolean) {
    setBusy(true);
    setError(undefined);
    try {
      await api.setTenantSettings({ [name]: value });
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-4">
      <div className="flex items-start gap-4">
        <Ico icon={icon} size={18} className="mt-0.5 text-muted" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[14.5px] font-semibold">{title}</h3>
            {beta && <Chip tone="amber">{t("settings.beta")}</Chip>}
          </div>
          <p className="mt-1 text-[13px] text-muted">{help}</p>
          {!owner && (
            <p className="mt-1 text-[12.5px] text-faint">
              {t("settings.ownersOnly")}
            </p>
          )}
        </div>
        <Toggle
          checked={on}
          disabled={busy || !owner}
          label={title}
          onChange={(value) =>
            value && warning ? setAsking(true) : void set(value)
          }
        />
      </div>
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
      {warning && (
        <Dialog
          open={asking}
          onClose={() => setAsking(false)}
          tone="amber"
          title={warning.title}
          footer={
            <>
              <Button data-autofocus onClick={() => setAsking(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setAsking(false);
                  void set(true);
                }}
              >
                {warning.confirm}
              </Button>
            </>
          }
        >
          {warning.body}
        </Dialog>
      )}
    </Card>
  );
}

/** The days a person may choose to keep their screenshots. */
const KEEP_DAYS = [1, 3, 7, 14, 30];

/** How long the screenshots of the signed-in person's finished tasks are kept:
 * theirs to choose, up to the deployment's most. */
function Retention() {
  const { t } = useI18n();
  const { api, me, refresh } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  if (!me) return null;
  const most = me.screenshot_retention_max;
  const days = [
    ...new Set([
      ...KEEP_DAYS.filter((d) => d <= most),
      most,
      me.screenshot_retention_days,
    ]),
  ].sort((a, b) => a - b);
  async function keep(value: string) {
    setBusy(true);
    setError(undefined);
    try {
      await api.setRetention(Number(value));
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card className="p-4">
      <div className="flex items-start gap-4">
        <Ico icon={Images} size={18} className="mt-0.5 text-muted" />
        <div className="min-w-0 flex-1">
          <h3 className="text-[14.5px] font-semibold">
            {t("retention.title")}
          </h3>
          <p className="mt-1 text-[13px] text-muted">
            {t("retention.help", { most })}
          </p>
          <Segmented
            label={t("retention.title")}
            value={String(me.screenshot_retention_days)}
            onChange={(value) => !busy && void keep(value)}
            className="mt-3"
            options={days.map((d) => ({
              value: String(d),
              label: t("retention.days", { count: d }),
            }))}
          />
          {error && (
            <p role="alert" className="mt-2 text-[13px] text-red">
              {error}
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}

/**
 * What the agent remembers for the signed-in person (§8.2): their own entries, which
 * every task they start sees. Only theirs; nobody else's tasks see them.
 */
/** Skills and plugins have a page of their own; a search here still finds it. */
function SkillsMoved() {
  const { t } = useI18n();
  return (
    <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 p-4">
      <Ico icon={Puzzle} size={18} className="text-muted" />
      <p className="min-w-0 flex-1 basis-60 text-[13.5px] text-muted">
        {t("customize.moved")}
      </p>
      <Link
        to="/customize"
        className="inline-flex items-center gap-1.5 text-[13.5px] font-medium text-accent"
      >
        {t("customize.open")}
        <Ico icon={ArrowRight} size={15} />
      </Link>
    </Card>
  );
}

function Memories() {
  const { t } = useI18n();
  const { api, me, refresh } = useAuth();
  const list = useAsync(() => api.memories(), [api]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function run(work: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await work();
      await list.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-4">
      <div className="flex items-start gap-4">
        <Ico icon={Bookmark} size={18} className="mt-0.5 text-muted" />
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <h3 className="text-[14.5px] font-semibold">
                {t("memories.title")}
              </h3>
              <p className="mt-1 text-[13px] text-muted">
                {t("memories.help")}
              </p>
            </div>
            <Toggle
              label={t("memories.auto")}
              checked={me?.auto_memory ?? true}
              disabled={busy}
              onChange={(on) =>
                void run(async () => {
                  await api.setAutoMemory(on);
                  await refresh();
                })
              }
            />
          </div>
          <p className="mt-2 text-[13px] text-muted">
            {t(
              (me?.auto_memory ?? true)
                ? "memories.auto.on"
                : "memories.auto.off",
            )}
          </p>
          <AppNotesToSkill memories={list.data ?? []} />
          <ul className="mt-3 divide-y divide-line">
            {(list.data ?? []).map((m) => (
              <li key={m.id} className="flex items-center gap-3 py-2">
                <span className="min-w-0 flex-1 text-[14px] break-words">
                  <span className="mr-1.5 text-[12.5px] text-faint">
                    {memoryKind(m, t)}
                    {m.by_agent &&
                      !(me?.auto_memory ?? true) &&
                      ` · ${t("memories.unused")}`}
                  </span>
                  {m.text}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void run(() => api.deleteMemory(m.id))}
                >
                  {t("memories.delete")}
                </Button>
              </li>
            ))}
            {list.data?.length === 0 && (
              <li className="py-2 text-[13px] text-faint">
                {t("memories.empty")}
              </li>
            )}
          </ul>
          <form
            className="mt-2 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (text.trim())
                void run(async () => {
                  await api.addMemory(text.trim());
                  setText("");
                });
            }}
          >
            <input
              aria-label={t("memories.title")}
              value={text}
              maxLength={300}
              onChange={(e) => setText(e.target.value)}
              placeholder={t("memories.placeholder")}
              className={inputClass}
            />
            <Button type="submit" busy={busy} disabled={!text.trim()}>
              {t("memories.add")}
            </Button>
          </form>
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </Card>
  );
}

/**
 * Which model providers may receive this workspace's screenshots and text .
 * Until an owner chooses, only the default model's provider. At least one stays on.
 */
function Providers() {
  const { t } = useI18n();
  const { api, me, refresh } = useAuth();
  const { models, reloadModels } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  if (!me) return null;
  const owner = me.role === "owner";
  const providers = [...new Set(models.map((m) => m.provider))].sort();
  if (providers.length < 2) return null; // nothing to choose
  const allowed = new Set(me.allowed_providers);

  async function set(provider: string, on: boolean) {
    const next = providers.filter((p) =>
      p === provider ? on : allowed.has(p),
    );
    setBusy(true);
    setError(undefined);
    try {
      await api.setTenantSettings({ allowed_providers: next });
      await Promise.all([refresh(), reloadModels()]);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-4">
      <div className="flex items-start gap-4">
        <Ico icon={Cloud} size={18} className="mt-0.5 text-muted" />
        <div className="min-w-0 flex-1">
          <h3 className="text-[14.5px] font-semibold">
            {t("settings.providers")}
          </h3>
          <p className="mt-1 text-[13px] text-muted">
            {t("settings.providers.help")}
          </p>
          {!owner && (
            <p className="mt-1 text-[12.5px] text-faint">
              {t("settings.ownersOnly")}
            </p>
          )}
          <ul className="mt-3 divide-y divide-line">
            {providers.map((p) => (
              <li key={p} className="flex items-center gap-3 py-2">
                <span className="flex-1 text-[14px]">{p}</span>
                <span className="text-[12.5px] text-faint">
                  {models
                    .filter((m) => m.provider === p)
                    .map((m) => m.name)
                    .join(", ")}
                </span>
                <Toggle
                  checked={allowed.has(p)}
                  disabled={
                    busy || !owner || (allowed.has(p) && allowed.size === 1)
                  }
                  label={p}
                  onChange={(on) => void set(p, on)}
                />
              </li>
            ))}
          </ul>
        </div>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </Card>
  );
}

type Section = "access" | "models" | "memory" | "skills" | "data";
const SECTIONS: Section[] = ["access", "models", "memory", "skills", "data"];

const sectionIcon: Record<Section, LucideIcon> = {
  access: ShieldAlert,
  models: Cpu,
  memory: Bookmark,
  skills: Puzzle,
  data: FileText,
};

/** One setting on the page: its section, the words a search looks in, and itself. */
interface Entry {
  id: string;
  section: Section;
  words: string[];
  node: ReactNode;
}

/** Tenant-wide settings (who may use full access, the fast risk check D29, which model
 * providers may receive the workspace's data, switching models), the person's own
 * memories and the way to the skills, in sections: a list of them beside the
 * page on wide screens, and a search over every setting's name and description. */
export default function Settings() {
  const { t } = useI18n();
  usePageTitle(t("settings.title"));
  const { me } = useAuth();
  const { models } = useWorkspace();
  const [query, setQuery] = useState("");
  const [current, setCurrent] = useState<Section>("access");
  // The list beside the page follows the section at the top while reading, and the
  // last one at the bottom of the page (it may be too short to reach the top). A
  // section chosen from the list stays chosen while the page scrolls to it.
  const settling = useRef(0);
  useEffect(() => {
    const found = SECTIONS.map((s) =>
      document.getElementById(`settings-${s}`),
    ).filter((el): el is HTMLElement => !!el);
    const page = found[0]?.closest("main");
    const name = (el: Element) => el.id.slice("settings-".length) as Section;
    const watch = new IntersectionObserver(
      (seen) => {
        if (Date.now() < settling.current) return;
        const top = seen
          .filter((e) => e.isIntersecting)
          .sort(
            (a, b) => a.boundingClientRect.top - b.boundingClientRect.top,
          )[0];
        if (top) setCurrent(name(top.target));
      },
      { rootMargin: "0px 0px -70% 0px" },
    );
    const bottom = () => {
      if (!page || Date.now() < settling.current) return;
      if (page.scrollTop + page.clientHeight >= page.scrollHeight - 2)
        setCurrent(name(found[found.length - 1]));
    };
    found.forEach((el) => watch.observe(el));
    page?.addEventListener("scroll", bottom, { passive: true });
    return () => {
      watch.disconnect();
      page?.removeEventListener("scroll", bottom);
    };
  }, [query, me]);
  if (!me) return null;
  const choosing = new Set(models.map((m) => m.provider)).size > 1;
  const notice = [
    t("notice.screenshots", { days: me.screenshot_retention_days }),
    t("notice.audit", { days: me.audit_retention_days }),
    t("notice.typed"),
    t("notice.payments"),
    t("notice.side"),
  ];
  const entries: Entry[] = [
    {
      id: "allow_full_access",
      section: "access",
      words: [t("devices.allowFull"), t("devices.allowFull.help")],
      node: (
        <Setting
          name="allow_full_access"
          on={me.allow_full_access}
          icon={ShieldAlert}
          title={t("devices.allowFull")}
          help={t("devices.allowFull.help")}
        />
      ),
    },
    {
      id: "fast_risk_check",
      section: "access",
      words: [t("settings.fast"), t("settings.fast.help")],
      node: (
        <Setting
          name="fast_risk_check"
          beta
          on={me.fast_risk_check}
          icon={Gauge}
          title={t("settings.fast")}
          help={t("settings.fast.help")}
          warning={{
            title: t("settings.fast.confirm.title"),
            confirm: t("settings.fast.confirm.ok"),
            body: (
              <div className="space-y-2.5 text-[13.5px] text-fg">
                <p className="flex items-center gap-2">
                  <Chip tone="amber">{t("settings.beta")}</Chip>
                  <span className="text-[12.5px] text-muted">
                    {t("settings.fast.beta")}
                  </span>
                </p>
                <p>{t("settings.fast.confirm.what")}</p>
                <p className="flex items-start gap-2 rounded-lg bg-amber-soft p-3">
                  <Ico
                    icon={AlertTriangle}
                    size={16}
                    className="mt-0.5 shrink-0 text-amber"
                  />
                  <span>{t("settings.fast.confirm.risk")}</span>
                </p>
                <ul className="list-disc space-y-1 pl-5 text-muted">
                  <li>{t("settings.fast.confirm.guard")}</li>
                  <li>{t("settings.fast.confirm.measured")}</li>
                  <li>{t("settings.fast.confirm.scope")}</li>
                </ul>
              </div>
            ),
          }}
        />
      ),
    },
    ...(choosing
      ? [
          {
            id: "providers",
            section: "models" as const,
            words: [
              t("settings.providers"),
              t("settings.providers.help"),
              ...models.flatMap((m) => [m.provider, m.name]),
            ],
            node: <Providers />,
          },
        ]
      : []),
    {
      id: "model_fallback",
      section: "models",
      words: [t("settings.fallback"), t("settings.fallback.help")],
      node: (
        <Setting
          name="model_fallback"
          on={me.model_fallback}
          icon={Shuffle}
          title={t("settings.fallback")}
          help={t("settings.fallback.help")}
        />
      ),
    },
    {
      id: "memories",
      section: "memory",
      words: [t("memories.title"), t("memories.help")],
      node: <Memories />,
    },
    {
      id: "skills",
      section: "skills",
      words: [
        t("customize.title"),
        t("skills.title"),
        t("skills.help"),
        t("customize.tab.plugins"),
        t("plugins.title"),
      ],
      node: <SkillsMoved />,
    },

    {
      id: "retention",
      section: "data",
      words: [t("retention.title"), t("retention.help", { most: 30 })],
      node: <Retention />,
    },
    {
      // What happens to people's data: said once, here, not in every task.
      id: "notice",
      section: "data",
      words: [t("notice.title"), ...notice],
      node: (
        <Card className="p-4">
          <ul className="list-disc space-y-2 pl-5 text-[13.5px] text-muted">
            {notice.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </Card>
      ),
    },
  ];
  const title = (section: Section) =>
    section === "data"
      ? t("notice.title")
      : t(`settings.section.${section}` as MessageKey);
  const words = query.trim().toLocaleLowerCase();
  const shown = words
    ? entries.filter((e) =>
        e.words.some((w) => w.toLocaleLowerCase().includes(words)),
      )
    : entries;
  const present = SECTIONS.filter((s) => shown.some((e) => e.section === s));
  const jump = (section: Section) => {
    setCurrent(section);
    settling.current = Date.now() + 800;
    document
      .getElementById(`settings-${section}`)
      ?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <PageBar>
        <h1 className="flex-1 text-[15px] font-semibold">
          {t("settings.title")}
        </h1>
      </PageBar>
      <main className="scroll-quiet flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-[980px] gap-8 px-4 py-6">
          <nav
            aria-label={t("settings.sections")}
            className="sticky top-6 hidden w-44 shrink-0 self-start md:block"
          >
            <ul className="space-y-0.5">
              {present.map((section) => (
                <li key={section}>
                  <button
                    type="button"
                    aria-current={current === section ? "true" : undefined}
                    onClick={() => jump(section)}
                    className={cn(
                      "flex h-8 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[13.5px]",
                      current === section
                        ? "bg-selected font-medium text-fg"
                        : "text-muted hover:bg-hovered hover:text-fg",
                    )}
                  >
                    <Ico icon={sectionIcon[section]} size={15} />
                    <span className="truncate">{title(section)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <div className="min-w-0 flex-1 space-y-6">
            <div className="space-y-3">
              <p className="text-[14px] text-muted">{t("settings.subtitle")}</p>
              <label className="flex h-9 items-center gap-2 rounded-lg border border-line bg-surface px-3 focus-within:border-accent/60">
                <Ico icon={Search} size={15} className="text-faint" />
                <span className="sr-only">{t("settings.search")}</span>
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t("settings.search")}
                  className="min-w-0 flex-1 bg-transparent text-[14px] placeholder:text-faint focus:outline-none"
                />
              </label>
            </div>
            {present.length === 0 && (
              <p className="py-8 text-center text-[13.5px] text-muted">
                {t("settings.noMatch", { query: query.trim() })}
              </p>
            )}
            {present.map((section) => (
              <section
                key={section}
                id={`settings-${section}`}
                aria-labelledby={`settings-${section}-title`}
                className="scroll-mt-6 space-y-3"
              >
                <h2
                  id={`settings-${section}-title`}
                  className="flex items-center gap-2 text-[13px] font-medium text-muted"
                >
                  <Ico icon={sectionIcon[section]} size={15} />
                  {title(section)}
                </h2>
                {shown
                  .filter((e) => e.section === section)
                  .map((e) => (
                    <div key={e.id}>{e.node}</div>
                  ))}
              </section>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
