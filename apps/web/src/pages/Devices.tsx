import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import {
  ArrowRight,
  Check,
  CircleAlert,
  Copy,
  CircleHelp,
  Hand,
  MoreHorizontal,
  Plus,
  Power,
  PowerOff,
  ShieldX,
  Smartphone,
} from "lucide-react";
import type {
  DeviceChange,
  DeviceView,
  GatewayCreated,
  GatewayView,
  TakeoverView,
} from "@/api/types.gen";
import { usePageTitle } from "@/lib/title";
import { useI18n, type MessageKey } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { cn, exactDate, relativeTime } from "@/lib/format";
import { useAsync } from "@/lib/useAsync";
import { byPool, takeoverable } from "@/lib/pool";
import { useWorkspace } from "@/lib/workspace";
import { PageBar } from "@/components/Shell";
import { loadTakeover, Takeover } from "@/components/takeover/lazy";
import {
  Button,
  Card,
  Chip,
  EmptyState,
  ErrorState,
  Field,
  Ico,
  Mono,
  PlatformIcon,
  Spinner,
  inputClass,
  type Tone,
} from "@/components/ui/base";
import { Dialog } from "@/components/ui/dialog";
import { InfoTip, Menu } from "@/components/ui/popover";

type Status =
  | "ready"
  | "busy"
  | "taken_over"
  | "not_enabled"
  | "disconnected"
  | "attention";

function statusOf(d: DeviceView): Status {
  if (!d.online || d.problems.includes("disconnected")) return "disconnected";
  if (d.problems.length) return "attention";
  if (!d.enabled) return "not_enabled";
  if (d.taken_over) return "taken_over";
  return d.task_id ? "busy" : "ready";
}

const statusTone: Record<Status, Tone> = {
  ready: "green",
  busy: "accent",
  taken_over: "red",
  not_enabled: "slate",
  disconnected: "slate",
  attention: "amber",
};

/** Who last enabled, disabled or took over the device, as a few words. */
function History({ device }: { device: DeviceView }) {
  const { t, locale } = useI18n();
  const { api } = useAuth();
  const [last, setLast] = useState<DeviceChange>();
  useEffect(() => {
    let live = true;
    api
      .deviceChanges(device.id)
      .then((changes) => live && setLast(changes[0]))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [api, device.id, device.enabled, device.taken_over]);
  if (!last) return null;
  return (
    <span className="text-faint">
      {" · "}
      {t(`devices.history.${last.change}` as MessageKey, {
        name: last.by,
        date: exactDate(last.at, locale),
      })}
    </span>
  );
}

/** Owners enable a new device under a name of their choice (disabling is in its menu). */
function Enabling({ device }: { device: DeviceView }) {
  const { t } = useI18n();
  const { api, me } = useAuth();
  const { devices } = useWorkspace();
  const [name, setName] = useState(device.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const owner = me?.role === "owner";

  async function act(work: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await work();
      await devices.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!owner) {
    return device.enabled ? null : (
      <p className="text-[13px] text-muted">{t("devices.memberNote")}</p>
    );
  }
  if (device.enabled) return null;
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        void act(() => api.enableDevice(device.id, { name }));
      }}
    >
      <label className="flex min-w-[180px] flex-1 flex-col gap-1 text-[12.5px] text-muted">
        {t("devices.name")}
        <input
          value={name}
          maxLength={128}
          onChange={(e) => setName(e.target.value)}
          className={cn(inputClass, "h-9 py-1 text-[14px]")}
        />
      </label>
      <Button type="submit" variant="primary" icon={Check} busy={busy}>
        {t("devices.enable")}
      </Button>
      {error && (
        <p role="alert" className="w-full text-[12.5px] text-red">
          {error}
        </p>
      )}
    </form>
  );
}

/** Control of a phone by hand, straight from the pool. A phone a task holds is taken
 * over on that task's page, which pauses the task first; a free one right here. */
function TakeOverButton({ device }: { device: DeviceView }) {
  const { t } = useI18n();
  const { api } = useAuth();
  const { devices } = useWorkspace();
  const navigate = useNavigate();
  const [takeover, setTakeover] = useState<TakeoverView>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const status = statusOf(device);
  // Once taken over the phone shows as such; the takeover stays open all the same. A
  // phone that is only locked can be taken over: the takeover tries waking it.
  const locked =
    status === "attention" && device.enabled && takeoverable(device);
  if (!takeover && status !== "ready" && status !== "busy" && !locked)
    return null;

  async function start() {
    if (device.task_id) {
      navigate(`/tasks/${device.task_id}?takeover=${device.id}`);
      return;
    }
    loadTakeover().catch(() => {}); // shown as the takeover opens, if still missing
    setBusy(true);
    setError(undefined);
    try {
      setTakeover(await api.takeoverDevice(device.id));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      void devices.reload();
    }
  }

  return (
    <div className="flex max-w-[45%] shrink-0 flex-col items-end gap-1">
      <Button size="sm" icon={Hand} busy={busy} onClick={() => void start()}>
        {t("devices.takeOver")}
      </Button>
      {error && (
        <span role="alert" className="text-right text-[12.5px] text-red">
          {error}
        </span>
      )}
      {takeover && (
        <Takeover
          key={takeover.takeover_id}
          takeover={takeover}
          device={device}
          outsideTask
          onAgain={() => void start()}
          onClosed={() => {
            setTakeover(undefined);
            void devices.reload();
          }}
        />
      )}
    </div>
  );
}

/** An owner's rarely used action on an enabled device, out of the way in a menu. */
function DeviceMenu({ device }: { device: DeviceView }) {
  const { t } = useI18n();
  const { api, me } = useAuth();
  const { devices } = useWorkspace();
  const [error, setError] = useState<string>();
  if (me?.role !== "owner" || !device.enabled) return null;
  const label = t("devices.menuFor", { name: device.name });
  return (
    <div className="flex shrink-0 flex-col items-end gap-1">
      <Menu
        label={label}
        minWidth={180}
        entries={[
          {
            id: "disable",
            label: t("devices.disable"),
            icon: PowerOff,
            danger: true,
            onSelect: () => {
              setError(undefined);
              api
                .disableDevice(device.id)
                .then(() => devices.reload())
                .catch((err: Error) => setError(err.message));
            },
          },
        ]}
        trigger={({ ref, open, toggle, onKeyDown }) => (
          <Button
            ref={ref}
            variant="ghost"
            size="sm"
            iconOnly
            icon={MoreHorizontal}
            aria-label={label}
            aria-haspopup="menu"
            aria-expanded={open}
            onClick={toggle}
            onKeyDown={onKeyDown}
          />
        )}
      />
      {error && (
        <span role="alert" className="text-right text-[12.5px] text-red">
          {error}
        </span>
      )}
    </div>
  );
}

function DeviceCard({
  device,
  highlight,
}: {
  device: DeviceView;
  highlight: boolean;
}) {
  const { t } = useI18n();
  const status = statusOf(device);
  return (
    <Card
      className={cn(
        "flex min-w-0 flex-col gap-3 p-4",
        highlight && "ring-2 ring-accent/40",
      )}
    >
      <div id={device.id} className="flex items-start gap-3 scroll-mt-20">
        <span
          aria-hidden
          className={cn(
            "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-sunken",
            status === "ready" || status === "busy" ? "text-fg" : "text-faint",
          )}
        >
          <PlatformIcon platform={device.platform} size={18} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-[15px] font-semibold">
              {device.name}
            </h3>
            <Chip tone={statusTone[status]}>
              {t(`devices.status.${status}` as MessageKey)}
            </Chip>
          </div>
          <p className="mt-0.5 truncate text-[12.5px] text-muted">
            {t(device.platform === "android" ? "common.android" : "common.ios")}
            <History device={device} />
          </p>
        </div>
        <TakeOverButton device={device} />
        <DeviceMenu device={device} />
      </div>
      {device.task_id && (
        <Link
          to={`/tasks/${device.task_id}`}
          className="flex items-center gap-2 rounded-lg bg-accent-soft/60 px-3 py-2 text-[13.5px] hover:bg-accent-soft"
        >
          <span className="shrink-0 text-muted">{t("devices.inTask")}</span>
          <span className="min-w-0 flex-1 truncate font-medium text-fg">
            {device.task_goal ?? t("devices.openTask")}
          </span>
          <Ico icon={ArrowRight} size={15} className="text-accent" />
        </Link>
      )}
      {device.problems.length > 0 && (
        <div className="rounded-lg bg-amber-soft/60 p-3">
          <h4 className="mb-1.5 text-[12.5px] font-medium text-amber">
            {t("devices.missing")}
          </h4>
          <ul className="space-y-1">
            {device.problems.map((p) => (
              <li key={p} className="flex items-start gap-2 text-[13.5px]">
                <Ico
                  icon={CircleAlert}
                  size={15}
                  className="mt-0.5 text-amber"
                />
                <span>{t(`fix.${p}` as MessageKey)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <Enabling device={device} />
    </Card>
  );
}

const POOL_TONE: Record<GatewayView["state"], Tone> = {
  enrolled: "green",
  waiting: "amber",
  disabled: "slate",
  revoked: "slate",
};

/** Owners add a device pool (a Gateway on a computer with phones) and manage it. */
function PoolsSection() {
  const { t, locale } = useI18n();
  const { api, me } = useAuth();
  const { devices } = useWorkspace();
  const pools = useAsync(() => api.gateways(), [api]);
  // Kept current in place, like the devices below: connected, phones, last seen.
  // Pools change with their devices: a Gateway connecting or going offline.
  const { devicesTick } = useWorkspace();
  const reloadPools = pools.reload;
  useEffect(() => {
    if (devicesTick) void reloadPools();
  }, [devicesTick, reloadPools]);
  const [adding, setAdding] = useState(false);
  const [guiding, setGuiding] = useState(false);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<GatewayCreated>();
  const [revoking, setRevoking] = useState<GatewayView>();
  const [error, setError] = useState<string>();
  const [copied, setCopied] = useState(false);
  if (me?.role !== "owner") return null;

  async function change(
    pool: GatewayView,
    what: "disable" | "enable" | "revoke",
  ) {
    setError(undefined);
    try {
      await api.changeGateway(pool.id, what);
      await pools.reload();
      await devices.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function add() {
    if (!name.trim()) return;
    setError(undefined);
    try {
      setCreated(await api.addGateway(name.trim()));
      await pools.reload();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const status = (pool: GatewayView) =>
    pool.state === "enrolled"
      ? pool.connected
        ? t("pools.state.connected")
        : t("pools.state.offline")
      : t(`pools.state.${pool.state}` as MessageKey);

  return (
    <section aria-labelledby="pools-title" className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h2 id="pools-title" className="text-[14.5px] font-semibold">
          {t("pools.title")}
        </h2>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            icon={CircleHelp}
            onClick={() => setGuiding(true)}
          >
            {t("pools.guide.button")}
          </Button>
          <Button
            size="sm"
            icon={Plus}
            onClick={() => {
              setName("");
              setCreated(undefined);
              setAdding(true);
            }}
          >
            {t("pools.add")}
          </Button>
        </div>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-red">
          {error}
        </p>
      )}
      <Card className="divide-y divide-line">
        {(pools.data ?? []).map((pool) => (
          <div key={pool.id} className="flex items-center gap-3 px-4 py-3">
            <span
              aria-hidden
              className={cn(
                "h-2 w-2 shrink-0 rounded-full",
                pool.state === "enrolled" && pool.connected
                  ? "bg-green"
                  : pool.state === "waiting"
                    ? "bg-amber"
                    : "bg-slate/50",
              )}
            />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={cn(
                    "truncate text-[14px] font-medium",
                    pool.state === "revoked" && "text-faint line-through",
                  )}
                >
                  {pool.name}
                </span>
                <Chip
                  tone={
                    pool.state === "enrolled" && !pool.connected
                      ? "slate"
                      : POOL_TONE[pool.state]
                  }
                >
                  {status(pool)}
                </Chip>
              </div>
              <p className="mt-0.5 truncate text-[12.5px] text-muted">
                {[
                  pool.host,
                  t("devices.count", { count: pool.devices }),
                  pool.state === "waiting" && pool.enroll_expires_at
                    ? t("pools.codeUntil", {
                        time: exactDate(pool.enroll_expires_at, locale),
                      })
                    : pool.last_seen_at
                      ? t("pools.lastSeen", {
                          time: relativeTime(pool.last_seen_at, locale),
                        })
                      : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            </div>
            {pool.state !== "revoked" && (
              <Menu
                label={t("pools.menuFor", { name: pool.name })}
                minWidth={200}
                entries={[
                  pool.state === "disabled"
                    ? {
                        id: "enable",
                        label: t("pools.enable"),
                        icon: Power,
                        onSelect: () => void change(pool, "enable"),
                      }
                    : {
                        id: "disable",
                        label: t("pools.disable"),
                        icon: PowerOff,
                        onSelect: () => void change(pool, "disable"),
                      },
                  { id: "s", separator: true },
                  {
                    id: "revoke",
                    label: t("pools.revoke"),
                    icon: ShieldX,
                    danger: true,
                    onSelect: () => setRevoking(pool),
                  },
                ]}
                trigger={({ ref, open, toggle, onKeyDown }) => (
                  <Button
                    ref={ref}
                    variant="ghost"
                    size="sm"
                    iconOnly
                    icon={MoreHorizontal}
                    aria-label={t("pools.menuFor", { name: pool.name })}
                    aria-haspopup="menu"
                    aria-expanded={open}
                    onClick={toggle}
                    onKeyDown={onKeyDown}
                  />
                )}
              />
            )}
          </div>
        ))}
        {pools.data?.length === 0 && (
          <p className="px-4 py-3 text-[13px] text-faint">{t("pools.none")}</p>
        )}
      </Card>

      <Dialog
        open={guiding}
        onClose={() => setGuiding(false)}
        title={t("pools.guide.title")}
        footer={
          <Button variant="primary" onClick={() => setGuiding(false)}>
            {t("pools.done")}
          </Button>
        }
      >
        <div className="space-y-3 text-[13.5px]">
          <p>{t("pools.guide.intro")}</p>
          <ol className="list-decimal space-y-3 pl-5">
            <li>
              {t("pools.guide.check")}
              <pre className="mt-1 overflow-x-auto rounded-md bg-sunken px-3 py-2 font-mono text-[12.5px]">
                witx-gateway --config gateway.toml doctor
              </pre>
            </li>
            <li>
              {t("pools.guide.setup")}
              <pre className="mt-1 overflow-x-auto rounded-md bg-sunken px-3 py-2 font-mono text-[12.5px]">
                witx-gateway --config gateway.toml setup
              </pre>
              <p className="mt-1 text-muted">{t("pools.guide.setup.body")}</p>
            </li>
            <li>
              {t("pools.guide.phone")}
              <ul className="mt-1 list-disc space-y-1 pl-5 text-muted">
                <li>{t("pools.guide.phone.android")}</li>
                <li>{t("pools.guide.phone.ios")}</li>
              </ul>
            </li>
            <li>{t("pools.guide.done")}</li>
          </ol>
        </div>
      </Dialog>
      <Dialog
        open={adding}
        onClose={() => setAdding(false)}
        title={created ? t("pools.created.title") : t("pools.add")}
        footer={
          created ? (
            <Button variant="primary" onClick={() => setAdding(false)}>
              {t("pools.done")}
            </Button>
          ) : (
            <>
              <Button onClick={() => setAdding(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                variant="primary"
                disabled={!name.trim()}
                onClick={() => void add()}
              >
                {t("pools.create")}
              </Button>
            </>
          )
        }
      >
        {created ? (
          <div className="space-y-3 text-[13.5px]">
            <p>{t("pools.created.body")}</p>
            <ol className="list-decimal space-y-2 pl-5">
              <li>
                {t("pools.step.config")}
                <pre className="mt-1 overflow-x-auto rounded-md bg-sunken px-3 py-2 font-mono text-[12.5px]">
                  {`cloud_url = "${window.location.origin}"`}
                </pre>
              </li>
              <li>
                {t("pools.step.enroll")}
                <pre className="mt-1 overflow-x-auto rounded-md bg-sunken px-3 py-2 font-mono text-[12.5px]">
                  witx-gateway --config gateway.toml enroll
                </pre>
              </li>
              <li>{t("pools.step.start")}</li>
            </ol>
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[12.5px] font-medium text-muted">
                  {t("pools.code")}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={copied ? Check : Copy}
                  onClick={() => {
                    void navigator.clipboard
                      ?.writeText(created.code)
                      .then(() => setCopied(true));
                  }}
                >
                  {copied ? t("pools.copied") : t("pools.copy")}
                </Button>
              </div>
              <Mono className="block rounded-md border border-line bg-surface px-3 py-2 text-[12.5px] break-all select-all">
                {created.code}
              </Mono>
              <p className="mt-1.5 text-[12px] text-muted">
                {t("pools.codeOnce")}
              </p>
            </div>
          </div>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              void add();
            }}
          >
            <p className="text-[13.5px]">{t("pools.help")}</p>
            <Field
              htmlFor="pool-name"
              label={t("pools.name")}
              hint={t("pools.name.hint")}
            >
              <input
                id="pool-name"
                data-autofocus
                value={name}
                maxLength={128}
                onChange={(e) => setName(e.target.value)}
                className={inputClass}
              />
            </Field>
          </form>
        )}
      </Dialog>
      <Dialog
        open={!!revoking}
        onClose={() => setRevoking(undefined)}
        tone="red"
        title={t("pools.revoke.title", { name: revoking?.name ?? "" })}
        footer={
          <>
            <Button data-autofocus onClick={() => setRevoking(undefined)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const pool = revoking;
                setRevoking(undefined);
                if (pool) void change(pool, "revoke");
              }}
            >
              {t("pools.revoke")}
            </Button>
          </>
        }
      >
        <p className="text-fg">{t("pools.revoke.body")}</p>
      </Dialog>
    </section>
  );
}

export default function Devices() {
  const { t } = useI18n();
  usePageTitle(t("devices.title"));
  const { devices } = useWorkspace();
  const location = useLocation();
  const highlight = location.hash.slice(1);
  // Brings the device of #<id> into view once it is there: not again on every refresh
  // of the list, which would pull the page back while the reader scrolls.
  const shownFor = useRef<string>(undefined);
  useEffect(() => {
    if (!highlight || shownFor.current === highlight) return;
    const card = document.getElementById(highlight);
    if (!card) return;
    card.scrollIntoView({ block: "center" });
    shownFor.current = highlight;
  }, [highlight, devices.data]);
  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <PageBar>
        <div className="flex flex-1 items-center gap-1">
          <h1 className="text-[15px] font-semibold">{t("devices.title")}</h1>
          <InfoTip label={t("devices.about")}>{t("devices.subtitle")}</InfoTip>
        </div>
      </PageBar>
      <main className="scroll-quiet flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[920px] space-y-6 px-4 py-6">
          {devices.error && (
            <ErrorState
              message={t("devices.error")}
              onRetry={() => void devices.reload()}
            />
          )}
          {!devices.data && !devices.error && <Spinner />}
          {devices.data?.length === 0 && (
            <EmptyState
              art={<Ico icon={Smartphone} size={28} className="text-faint" />}
              title={t("devices.emptyTitle")}
              body={t("devices.empty")}
            />
          )}
          {byPool(devices.data ?? []).map(([pool, members]) => (
            <section key={pool} aria-label={pool}>
              <h2 className="mb-2 flex items-center gap-2 text-[13px] text-muted">
                <span
                  aria-hidden
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    members.some((d) => d.online) ? "bg-green" : "bg-slate/60",
                  )}
                />
                <Mono className="text-[13px]">{pool || t("devices.pool")}</Mono>
                <span className="text-faint">
                  · {t("devices.count", { count: members.length })}
                </span>
              </h2>
              <div className="grid gap-3 md:grid-cols-2">
                {members.map((d) => (
                  <DeviceCard
                    key={d.id}
                    device={d}
                    highlight={d.id === highlight}
                  />
                ))}
              </div>
            </section>
          ))}
          {/* Adding and managing pools is rare, and an owner's: after the phones. */}
          <PoolsSection />
        </div>
      </main>
    </div>
  );
}
