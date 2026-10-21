import { useState } from "react";
import { useNavigate } from "react-router";
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  CornerDownLeft,
  History,
  Pencil,
  Play,
  Plus,
  Puzzle,
  Search,
  Trash2,
} from "lucide-react";
import type {
  MemoryView,
  SkillView,
  SkillVersionView,
  TaskDetail,
} from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { lineDiff } from "@/lib/diff";
import { cn } from "@/lib/format";
import type { SkillDraft } from "@/lib/timeline";
import { useAsync } from "@/lib/useAsync";
import Markdown from "@/components/Markdown";
import {
  Button,
  Card,
  Chip,
  Field,
  Ico,
  Mono,
  RelTime,
  inputClass,
} from "@/components/ui/base";
import { Dialog } from "@/components/ui/dialog";
import type { SlashCommand } from "@/components/SlashMenu";

/**
 * The skills offered when a message names one with "$": naming one makes the
 * agent load it first, and is the only way to use one that is for naming only.
 */
export function useSkillCommands(): SlashCommand[] {
  const { api } = useAuth();
  const list = useAsync(() => api.skills(), [api]);
  return (list.data?.skills ?? []).map((skill) => ({
    name: `$${skill.name}`,
    hint: skill.description,
    icon: Puzzle,
  }));
}

/** How many notes about one app make it worth turning them into a skill. */
const NOTES_FOR_A_SKILL = 3;

/**
 * The apps the agent has kept several notes about, each with a way to turn them
 * into one skill: a `/skill` task without a phone, drafted from those notes only.
 * The draft is reviewed and saved as any other; the notes stay until the person
 * deletes them.
 */
export function AppNotesToSkill({ memories }: { memories: MemoryView[] }) {
  const { t } = useI18n();
  const { api } = useAuth();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const byApp = new Map<string, string[]>();
  for (const m of memories)
    if (m.kind === "app" && m.app)
      byApp.set(m.app, [...(byApp.get(m.app) ?? []), m.text]);
  const apps = [...byApp].filter(
    ([, notes]) => notes.length >= NOTES_FOR_A_SKILL,
  );
  if (apps.length === 0) return null;

  async function start(app: string, notes: string[]) {
    setBusy(app);
    setError(undefined);
    let request = `/skill ${t("memories.toSkill.request", { app })}`;
    for (const note of notes) {
      if (request.length + note.length + 3 > REQUEST_MOST) break;
      request += `\n- ${note}`;
    }
    try {
      const created = await api.createTask({
        goal: request,
        device_ids: [],
        permission_mode: "ask",
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      navigate(`/tasks/${created.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(undefined);
    }
  }

  return (
    <div className="mt-3 space-y-2">
      {apps.map(([app, notes]) => (
        <div
          key={app}
          className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg bg-sunken p-3 text-[13px]"
        >
          <Ico icon={Puzzle} size={15} className="text-accent" />
          <span className="min-w-0 flex-1 break-words">
            {t("memories.toSkill.notes", {
              app,
              count: String(notes.length),
            })}
          </span>
          <Button
            size="sm"
            busy={busy === app}
            disabled={!!busy}
            onClick={() => void start(app, notes)}
          >
            {t("memories.toSkill")}
          </Button>
        </div>
      ))}
      {error && (
        <p role="alert" className="text-[13px] text-red">
          {error}
        </p>
      )}
    </div>
  );
}

/** A skill's name in the plugin `my`, as the server takes it. */
const NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** What the person is writing: a new skill, or a change to one of theirs. */
interface Draft {
  name: string;
  description: string;
  apps: string;
  body: string;
  /** Used only when the person names it ($name), never on the agent's judgement. */
  namedOnly: boolean;
  /** The saved skill it changes; none for a new one. */
  current?: SkillView;
}

const appsOf = (text: string) =>
  text
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);

/** A skill's own name, without its plugin. */
const shortName = (skill: { name: string }) =>
  skill.name.split(":")[1] ?? skill.name;

/**
 * The skills the agent can load: every installed plugin's, the plugins that are
 * not loaded and why, and the person's own, which an owner writes, changes,
 * restores and deletes here. A list to search, with the skill being read beside it on
 * wide screens and in its place on narrow ones. Nothing is saved before the
 * person has seen what changes.
 */
export function Skills({
  open,
  onOpen,
}: {
  /** The skill being read (plugin:skill). */
  open?: string;
  onOpen: (name?: string) => void;
}) {
  const { t } = useI18n();
  const { api, me } = useAuth();
  const owner = me?.role === "owner";
  const list = useAsync(() => api.skills(), [api]);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<Draft>();
  const [reviewing, setReviewing] = useState(false);
  const [history, setHistory] = useState<SkillView>();
  const [deleting, setDeleting] = useState<SkillView>();

  const skills = list.data?.skills ?? [];
  const words = query.trim().toLowerCase();
  const shown = skills.filter(
    (s) =>
      !words ||
      [s.name, s.description, ...s.apps].some((w) =>
        w.toLowerCase().includes(words),
      ),
  );
  const plugins = [...new Set(shown.map((s) => s.plugin))].sort((a, b) =>
    a === "my" ? -1 : b === "my" ? 1 : a.localeCompare(b),
  );
  const reading = skills.find((s) => s.name === open);
  const edit = (skill: SkillView) => {
    setDraft({
      name: shortName(skill),
      description: skill.description,
      apps: skill.apps.join(", "),
      body: skill.body,
      namedOnly: skill.invoke === "user",
      current: skill,
    });
    setReviewing(false);
  };
  const restore = (skill: SkillView, version: SkillVersionView) => {
    setHistory(undefined);
    setDraft({
      name: shortName(skill),
      description: version.description,
      apps: version.apps.join(", "),
      body: version.body,
      namedOnly: version.invoke === "user",
      current: skill,
    });
    setReviewing(true);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <p className="min-w-0 flex-1 basis-72 text-[13px] text-muted">
          {t("skills.help")}
          {!owner && (
            <span className="mt-1 block text-[12.5px] text-faint">
              {t("settings.ownersOnly")}
            </span>
          )}
        </p>
        {owner && (
          <Button
            size="sm"
            variant="primary"
            icon={Plus}
            onClick={() => {
              setDraft({
                name: "",
                description: "",
                apps: "",
                body: "",
                namedOnly: false,
              });
              setReviewing(false);
            }}
          >
            {t("skills.new")}
          </Button>
        )}
      </div>
      {list.error && (
        <p role="alert" className="text-[13px] text-red">
          {list.error.message}
        </p>
      )}
      {(list.data?.not_loaded ?? []).map((p) => (
        <p
          key={p.plugin}
          className="flex items-start gap-2 rounded-lg bg-amber-soft p-3 text-[13px]"
        >
          <Ico icon={AlertTriangle} size={15} className="mt-0.5 text-amber" />
          <span className="min-w-0 break-words">
            {t("skills.notLoaded", { plugin: p.plugin })} {p.reason}
          </span>
        </p>
      ))}
      {list.data && skills.length === 0 ? (
        <Card className="p-4 text-[13px] text-faint">{t("skills.empty")}</Card>
      ) : (
        <div className="md:grid md:grid-cols-[minmax(0,320px)_minmax(0,1fr)] md:items-start md:gap-4">
          <Card className={cn("p-2", reading && "hidden md:block")}>
            <label className="flex h-9 cursor-text items-center gap-2 rounded-lg px-2 text-muted focus-within:bg-selected">
              <Ico icon={Search} size={15} />
              <span className="sr-only">{t("skills.search")}</span>
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("skills.search")}
                className="min-w-0 flex-1 bg-transparent text-[13.5px] text-fg placeholder:text-muted focus:outline-none"
              />
            </label>
            {list.data && shown.length === 0 && (
              <p className="px-2 py-3 text-[13px] text-faint">
                {t("skills.noMatch")}
              </p>
            )}
            {plugins.map((plugin) => {
              const theirs = shown.filter((s) => s.plugin === plugin);
              return (
                <div key={plugin} className="mt-2">
                  <p className="px-2 text-[12.5px] text-faint">
                    {plugin === "my"
                      ? t("skills.mine", { version: theirs[0].version })
                      : t("skills.from", {
                          plugin,
                          marketplace: theirs[0].marketplace,
                          version: theirs[0].version,
                        })}
                  </p>
                  <ul className="mt-1 space-y-0.5">
                    {theirs.map((skill) => (
                      <li key={skill.name}>
                        <button
                          type="button"
                          aria-current={
                            open === skill.name ? "true" : undefined
                          }
                          onClick={() => onOpen(skill.name)}
                          className={cn(
                            "w-full rounded-lg px-2 py-1.5 text-left",
                            open === skill.name
                              ? "bg-selected"
                              : "hover:bg-hovered",
                          )}
                        >
                          <Mono className="text-fg">{skill.name}</Mono>
                          {skill.invoke === "user" && (
                            <Chip className="ml-2">
                              {t("skills.namedOnly.chip")}
                            </Chip>
                          )}
                          <span className="line-clamp-2 block text-[13px] text-muted">
                            {skill.description}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </Card>
          {reading ? (
            <Card className="p-4">
              <Button
                size="sm"
                variant="ghost"
                icon={ChevronLeft}
                className="-ml-2 mb-2 md:hidden"
                onClick={() => onOpen(undefined)}
              >
                {t("skills.back")}
              </Button>
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1">
                  <h2 className="font-mono text-[15px] font-semibold break-all">
                    {reading.name}
                  </h2>
                  <p className="text-[12.5px] text-faint">
                    {reading.plugin === "my"
                      ? t("skills.mine", { version: reading.version })
                      : t("skills.from", {
                          plugin: reading.plugin,
                          marketplace: reading.marketplace,
                          version: reading.version,
                        })}
                  </p>
                </div>
                {reading.editable && owner && (
                  <div className="flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      icon={Pencil}
                      onClick={() => edit(reading)}
                    >
                      {t("skills.edit")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={History}
                      onClick={() => setHistory(reading)}
                    >
                      {t("skills.history")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Trash2}
                      onClick={() => setDeleting(reading)}
                    >
                      {t("skills.delete")}
                    </Button>
                  </div>
                )}
              </div>
              <p className="mt-3 text-[14px]">{reading.description}</p>
              {(reading.invoke === "user" || reading.apps.length > 0) && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {reading.invoke === "user" && (
                    <Chip>{t("skills.namedOnly.chip")}</Chip>
                  )}
                  {reading.apps.map((app) => (
                    <Chip key={app}>{app}</Chip>
                  ))}
                </div>
              )}
              <div className="mt-3 rounded-lg border border-line p-3 text-[13.5px]">
                <Markdown text={reading.body} />
              </div>
            </Card>
          ) : (
            <Card className="hidden p-6 text-center text-[13px] text-faint md:block">
              {t("skills.pick")}
            </Card>
          )}
        </div>
      )}
      {draft && (
        <Editor
          draft={draft}
          taken={skills.filter((s) => s.editable).map(shortName)}
          reviewing={reviewing}
          setReviewing={setReviewing}
          onChange={setDraft}
          onClose={() => setDraft(undefined)}
          onSaved={(skill) => {
            setDraft(undefined);
            onOpen(skill.name);
            void list.reload();
          }}
        />
      )}
      {history && (
        <Versions
          skill={history}
          onClose={() => setHistory(undefined)}
          onRestore={(version) => restore(history, version)}
        />
      )}
      {deleting && (
        <Delete
          skill={deleting}
          onClose={() => setDeleting(undefined)}
          onDeleted={() => {
            setDeleting(undefined);
            onOpen(undefined);
            void list.reload();
          }}
        />
      )}
    </div>
  );
}

/**
 * A skill the agent drafted in the conversation. Nothing is saved until the person
 * has reviewed it, as on the settings page; to change it, they can edit it there or tell
 * the agent in the message box, and try it first in a task of its own, in plan mode.
 * Once the agent drafts the same skill again, the older card shrinks to its name.
 */
export function SkillDraftCard({
  draft,
  superseded,
  task,
}: {
  draft: SkillDraft;
  superseded: boolean;
  task: TaskDetail;
}) {
  const { t } = useI18n();
  const { api, me } = useAuth();
  const owner = me?.role === "owner";
  const list = useAsync(() => api.skills(), [api]);
  const [editing, setEditing] = useState<Draft>();
  const [reviewing, setReviewing] = useState(true);
  const [saved, setSaved] = useState<SkillView>();
  const [trying, setTrying] = useState(false);

  const mine = (list.data?.skills ?? []).filter((s) => s.editable);
  const current = mine.find((s) => s.name === draft.name);
  // Saved as drafted, here or before.
  const same =
    current &&
    current.description === draft.description &&
    current.body === draft.body &&
    current.apps.join(",") === draft.apps.join(",");
  const done = saved ?? (same ? current : undefined);

  // Drafted again below: only its name stays, faint.
  if (superseded && !done) {
    return (
      <section
        aria-label={t("skillDraft.title", { name: draft.name })}
        className="flex items-center gap-1.5 text-[12.5px] text-faint"
      >
        <Ico icon={Puzzle} size={14} />
        <span className="truncate line-through">{draft.name}</span>
        <span>· {t("skillDraft.superseded")}</span>
      </section>
    );
  }

  return (
    <section
      aria-label={t("skillDraft.title", { name: draft.name })}
      className="rounded-xl border border-accent/40 bg-surface px-4 py-3"
    >
      <div className="mb-1 flex items-center gap-1.5 text-[12.5px] font-medium text-accent">
        <Ico icon={Puzzle} size={14} />
        {t("skillDraft.title", { name: draft.name })}
      </div>
      <p className="text-[15px] break-words">{draft.description}</p>
      {draft.apps.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {draft.apps.map((app) => (
            <Chip key={app}>{app}</Chip>
          ))}
        </div>
      )}
      <div className="mt-2 max-h-72 overflow-auto rounded-lg border border-line p-3 text-[13.5px]">
        <Markdown text={draft.body} />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        {done ? (
          <span className="flex items-center gap-1.5 text-[13px] text-green">
            <Ico icon={Check} size={14} />
            {t("skillDraft.saved", { name: done.name, version: done.version })}
          </span>
        ) : owner ? (
          <>
            <Button
              size="sm"
              variant="primary"
              icon={Check}
              disabled={!list.data}
              onClick={() => {
                setEditing({
                  name: shortName(draft),
                  description: draft.description,
                  apps: draft.apps.join(", "),
                  body: draft.body,
                  namedOnly: current?.invoke === "user",
                  current,
                });
                setReviewing(true);
              }}
            >
              {t("skillDraft.review")}
            </Button>
            <Button size="sm" icon={Play} onClick={() => setTrying(true)}>
              {t("skillDraft.try")}
            </Button>
            <Button
              size="sm"
              icon={CornerDownLeft}
              onClick={() => document.getElementById("note")?.focus()}
            >
              {t("skillDraft.change")}
            </Button>
            <span className="text-[12.5px] text-muted">
              {current
                ? t("skillDraft.replaces", { name: current.name })
                : t("skillDraft.hint")}
            </span>
          </>
        ) : (
          <span className="text-[12.5px] text-faint">
            {t("skillDraft.ownersOnly")}
          </span>
        )}
      </div>
      {list.error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {list.error.message}
        </p>
      )}
      {trying && (
        <Trial draft={draft} task={task} onClose={() => setTrying(false)} />
      )}
      {editing && (
        <Editor
          draft={editing}
          taken={mine.map(shortName)}
          reviewing={reviewing}
          setReviewing={setReviewing}
          onChange={setEditing}
          onClose={() => setEditing(undefined)}
          onSaved={(skill) => {
            setEditing(undefined);
            setSaved(skill);
            void list.reload();
          }}
        />
      )}
    </section>
  );
}

/** The most a task's first message holds (TaskSpec.goal). */
const REQUEST_MOST = 4000;

/**
 * Trying a draft before it is saved: a new task in plan mode on this task's phones,
 * asked for one thing with the draft as the way to do it. The agent only looks, and its
 * plan says where the draft matches the screens and where it does not; approving the
 * plan carries it out.
 */
function Trial({
  draft,
  task,
  onClose,
}: {
  draft: SkillDraft;
  task: TaskDetail;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const navigate = useNavigate();
  const [ask, setAsk] = useState(draft.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const request = `/plan ${t("skillDraft.try.request", {
    name: draft.name,
    ask: ask.trim(),
  })}\n\n${draft.body}`;
  const tooLong = request.length > REQUEST_MOST;

  async function start() {
    setBusy(true);
    setError(undefined);
    try {
      const created = await api.createTask({
        goal: request,
        device_ids: task.device_ids,
        model: task.model,
        permission_mode: task.permission_mode,
        reasoning_effort: task.reasoning_effort,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      navigate(`/tasks/${created.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title={t("skillDraft.try.title", { name: draft.name })}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            variant="primary"
            icon={Play}
            busy={busy}
            disabled={!ask.trim() || tooLong}
            onClick={() => void start()}
          >
            {t("skillDraft.try.start")}
          </Button>
        </>
      }
    >
      <p className="mb-3 text-[13.5px] text-muted">
        {t("skillDraft.try.help")}
      </p>
      <Field
        label={t("skillDraft.try.ask")}
        hint={tooLong ? t("skillDraft.try.tooLong") : t("skillDraft.try.hint")}
        htmlFor="skill-try"
      >
        <textarea
          id="skill-try"
          data-autofocus
          value={ask}
          rows={3}
          onChange={(e) => setAsk(e.target.value)}
          className={cn(inputClass, "resize-y")}
        />
      </Field>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-red">
          {error}
        </p>
      )}
    </Dialog>
  );
}

/** Writing a skill, then reviewing what will change before it is saved. */
function Editor({
  draft,
  taken,
  reviewing,
  setReviewing,
  onChange,
  onClose,
  onSaved,
}: {
  draft: Draft;
  taken: string[];
  reviewing: boolean;
  setReviewing: (on: boolean) => void;
  onChange: (draft: Draft) => void;
  onClose: () => void;
  onSaved: (skill: SkillView) => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const fresh = !draft.current;
  const nameProblem = !fresh
    ? undefined
    : draft.name && !NAME.test(draft.name)
      ? t("skills.name.invalid")
      : taken.includes(draft.name)
        ? t("skills.name.taken")
        : undefined;
  const ready =
    !!draft.name &&
    !nameProblem &&
    !!draft.description.trim() &&
    !!draft.body.trim();
  const set = (part: Partial<Draft>) => onChange({ ...draft, ...part });

  async function save() {
    setBusy(true);
    setError(undefined);
    try {
      onSaved(
        await api.saveSkill(draft.name, {
          description: draft.description,
          apps: appsOf(draft.apps),
          body: draft.body,
          invoke: draft.namedOnly ? "user" : "auto",
        }),
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const title = fresh
    ? t("skills.new.title")
    : t("skills.edit.title", { name: `my:${draft.name}` });
  return (
    <Dialog
      open
      wide
      onClose={onClose}
      title={title}
      footer={
        reviewing ? (
          <>
            <Button onClick={() => setReviewing(false)}>
              {t("skills.back")}
            </Button>
            <Button variant="primary" busy={busy} onClick={() => void save()}>
              {t("skills.save")}
            </Button>
          </>
        ) : (
          <>
            <Button onClick={onClose}>{t("common.cancel")}</Button>
            <Button
              variant="primary"
              disabled={!ready}
              onClick={() => {
                setError(undefined);
                setReviewing(true);
              }}
            >
              {t("skills.review")}
            </Button>
          </>
        )
      }
    >
      {reviewing ? (
        <Review draft={draft} />
      ) : (
        <div className="space-y-3">
          {fresh && (
            <Field
              label={t("skills.name")}
              hint={nameProblem ?? t("skills.name.hint")}
              htmlFor="skill-name"
            >
              <input
                id="skill-name"
                data-autofocus
                value={draft.name}
                maxLength={64}
                onChange={(e) => set({ name: e.target.value })}
                placeholder="clear-shopping-list"
                className={cn(inputClass, "font-mono")}
              />
            </Field>
          )}
          <Field
            label={t("skills.description")}
            hint={t("skills.description.hint")}
            htmlFor="skill-description"
          >
            <input
              id="skill-description"
              value={draft.description}
              maxLength={300}
              onChange={(e) => set({ description: e.target.value })}
              className={inputClass}
            />
          </Field>
          <Field
            label={t("skills.apps")}
            hint={t("skills.apps.hint")}
            htmlFor="skill-apps"
          >
            <input
              id="skill-apps"
              value={draft.apps}
              onChange={(e) => set({ apps: e.target.value })}
              placeholder="com.tencent.mm"
              className={cn(inputClass, "font-mono")}
            />
          </Field>
          <Field
            label={t("skills.body")}
            hint={t("skills.body.hint")}
            htmlFor="skill-body"
          >
            <textarea
              id="skill-body"
              value={draft.body}
              maxLength={20000}
              rows={14}
              onChange={(e) => set({ body: e.target.value })}
              className={cn(inputClass, "resize-y font-mono leading-relaxed")}
            />
          </Field>
          <label className="flex items-start gap-2 text-[13.5px]">
            <input
              type="checkbox"
              checked={draft.namedOnly}
              onChange={(e) => set({ namedOnly: e.target.checked })}
              className="mt-1"
            />
            <span>
              {t("skills.namedOnly")}
              <span className="block text-[12.5px] text-muted">
                {t("skills.namedOnly.hint", {
                  name: `$my:${draft.name || "…"}`,
                })}
              </span>
            </span>
          </label>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-red">
          {error}
        </p>
      )}
    </Dialog>
  );
}

/** What saving will change: the agent follows this text in every task that loads it. */
function Review({ draft }: { draft: Draft }) {
  const { t } = useI18n();
  const before = draft.current;
  const changed = (a: string, b: string) => a.trim() !== b.trim();
  const apps = appsOf(draft.apps).join(", ");
  return (
    <div className="space-y-3 text-[13.5px]">
      <p className="text-muted">{t("skills.review.help")}</p>
      {(!before || changed(before.description, draft.description)) && (
        <p>
          <span className="text-faint">{t("skills.description")}: </span>
          {draft.description.trim()}
        </p>
      )}
      {(!before ? apps : changed(before.apps.join(", "), apps)) && (
        <p>
          <span className="text-faint">{t("skills.apps")}: </span>
          <Mono>{apps || "—"}</Mono>
        </p>
      )}
      {(before
        ? (before.invoke === "user") !== draft.namedOnly
        : draft.namedOnly) && (
        <p>
          {draft.namedOnly ? t("skills.namedOnly") : t("skills.namedOnly.off")}
        </p>
      )}
      <pre
        aria-label={t("skills.changes")}
        className="max-h-[50vh] overflow-auto rounded-lg border border-line p-3 font-mono text-[12.5px] leading-relaxed whitespace-pre-wrap"
      >
        {lineDiff(before?.body ?? "", draft.body.trim()).map((line, i) => (
          <div
            key={i}
            className={cn(
              line.kind === "added" && "bg-green-soft text-green",
              line.kind === "removed" && "bg-red-soft text-red",
              line.kind === "same" && before && "text-muted",
            )}
          >
            {line.kind === "added"
              ? "+ "
              : line.kind === "removed"
                ? "- "
                : "  "}
            {line.text}
          </div>
        ))}
      </pre>
      {before && before.body === draft.body.trim() && (
        <p className="text-faint">{t("skills.review.sameText")}</p>
      )}
    </div>
  );
}

/** A skill's saved versions, newest first; an older one can be restored. */
function Versions({
  skill,
  onClose,
  onRestore,
}: {
  skill: SkillView;
  onClose: () => void;
  onRestore: (version: SkillVersionView) => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const name = shortName(skill);
  const versions = useAsync(() => api.skillHistory(name), [api, name]);
  return (
    <Dialog
      open
      onClose={onClose}
      title={t("skills.history.title", { name: skill.name })}
      footer={<Button onClick={onClose}>{t("common.close")}</Button>}
    >
      {versions.error && (
        <p role="alert" className="text-[13px] text-red">
          {versions.error.message}
        </p>
      )}
      <ul className="divide-y divide-line">
        {(versions.data ?? []).map((v, i) => (
          <li key={v.commit} className="flex items-center gap-3 py-2">
            <span className="min-w-0 flex-1">
              <Mono className="text-fg">v{v.version}</Mono>
              <span className="ml-2 text-[12.5px] text-faint">
                <RelTime iso={v.saved_at} />
              </span>
              <span className="block truncate text-[13px] text-muted">
                {v.description}
              </span>
            </span>
            {i === 0 ? (
              <span className="text-[12.5px] text-faint">
                {t("skills.history.current")}
              </span>
            ) : (
              <Button size="sm" onClick={() => onRestore(v)}>
                {t("skills.history.restore")}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </Dialog>
  );
}

function Delete({
  skill,
  onClose,
  onDeleted,
}: {
  skill: SkillView;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [purge, setPurge] = useState(false);
  async function remove() {
    setBusy(true);
    setError(undefined);
    try {
      await api.deleteSkill(shortName(skill), purge);
      onDeleted();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open
      tone="red"
      onClose={onClose}
      title={t("skills.delete.title", { name: skill.name })}
      footer={
        <>
          <Button data-autofocus onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="danger" busy={busy} onClick={() => void remove()}>
            {purge ? t("skills.purge.confirm") : t("skills.delete")}
          </Button>
        </>
      }
    >
      <p className="text-[13.5px]">
        {purge ? t("skills.purge.help") : t("skills.delete.help")}
      </p>
      <label className="mt-3 flex items-start gap-2 text-[13.5px]">
        <input
          type="checkbox"
          checked={purge}
          onChange={(e) => setPurge(e.target.checked)}
          className="mt-1"
        />
        <span>
          {t("skills.purge")}
          <span className="block text-[12.5px] text-muted">
            {t("skills.purge.hint")}
          </span>
        </span>
      </label>
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </Dialog>
  );
}
