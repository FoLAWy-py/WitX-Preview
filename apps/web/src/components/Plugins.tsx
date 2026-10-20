import { useState } from "react";
import { Download, RefreshCw, Store, Trash2 } from "lucide-react";
import type { InstalledPluginView, PluginOfferView } from "@/api/types.gen";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { lineDiff } from "@/lib/diff";
import { cn } from "@/lib/format";
import { useAsync } from "@/lib/useAsync";
import { Button, Card, Ico, Mono } from "@/components/ui/base";
import { Dialog } from "@/components/ui/dialog";

/**
 * Plugins from marketplaces: the installed ones with the skills each brings
 *, and what a marketplace offers once the person looks (that fetches it). An owner
 * installs, updates or removes one; nothing is installed before they have read its
 * text, or what changes, and the server installs only what they read (its commit and
 * digest).
 */
export function Plugins({ onSkill }: { onSkill: (name: string) => void }) {
  const { t } = useI18n();
  const { api, me } = useAuth();
  const owner = me?.role === "owner";
  const state = useAsync(() => api.marketplaces(), [api]);
  const skills = useAsync(() => api.skills(), [api]);
  const [browsing, setBrowsing] = useState<string>();
  const [reading, setReading] = useState<{
    marketplace: string;
    offer: PluginOfferView;
  }>();
  const [removing, setRemoving] = useState<InstalledPluginView>();
  const installed = state.data?.installed ?? [];
  const marketplaces = state.data?.marketplaces ?? [];
  const changed = () => {
    void state.reload();
    void skills.reload();
  };

  return (
    <div className="space-y-6">
      <p className="text-[13px] text-muted">{t("plugins.help")}</p>
      {state.error && (
        <p role="alert" className="text-[13px] text-red">
          {state.error.message}
        </p>
      )}
      <section aria-label={t("plugins.installedTitle")}>
        <h2 className="mb-2 text-[13px] text-muted">
          {t("plugins.installedTitle")}{" "}
          <span className="text-faint">· {installed.length}</span>
        </h2>
        {state.data && installed.length === 0 && (
          <Card className="p-4 text-[13px] text-faint">
            {t("plugins.noneInstalled")}
          </Card>
        )}
        <ul className="space-y-2">
          {installed.map((p) => {
            const its = (skills.data?.skills ?? []).filter(
              (s) => s.plugin === p.name,
            );
            const trouble = skills.data?.not_loaded.find(
              (n) => n.plugin === p.name,
            );
            return (
              <li key={p.name}>
                <Card className="flex items-start gap-3 p-4">
                  <Ico icon={Store} size={17} className="mt-0.5 text-muted" />
                  <div className="min-w-0 flex-1 text-[13px]">
                    <Mono className="text-[14px] font-semibold text-fg">
                      {p.name}
                    </Mono>
                    <p className="text-muted">
                      {t("plugins.installed", {
                        version: p.version,
                        marketplace: p.marketplace,
                      })}{" "}
                      · <Mono>{p.commit.slice(0, 12)}</Mono>
                    </p>
                    {trouble && (
                      <p className="mt-2 rounded-lg bg-amber-soft p-2 break-words">
                        {t("skills.notLoaded", { plugin: p.name })}{" "}
                        {trouble.reason}
                      </p>
                    )}
                    {its.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {its.map((s) => (
                          <button
                            key={s.name}
                            type="button"
                            title={s.description}
                            onClick={() => onSkill(s.name)}
                            className="rounded-md border border-line px-2 py-0.5 font-mono text-[12.5px] text-muted hover:bg-hovered hover:text-fg"
                          >
                            {s.name}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {owner && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Trash2}
                      onClick={() => setRemoving(p)}
                    >
                      {t("plugins.remove")}
                    </Button>
                  )}
                </Card>
              </li>
            );
          })}
        </ul>
      </section>
      <section aria-label={t("plugins.title")}>
        <h2 className="mb-2 text-[13px] text-muted">{t("plugins.title")}</h2>
        {state.data && marketplaces.length === 0 && (
          <Card className="p-4 text-[13px] text-faint">
            {t("plugins.noMarketplace")}
          </Card>
        )}
        <div className="space-y-2">
          {marketplaces.map((m) => (
            <Marketplace
              key={m.name}
              name={m.name}
              source={m.source}
              installed={installed}
              owner={owner}
              open={browsing === m.name}
              onOpen={() =>
                setBrowsing(browsing === m.name ? undefined : m.name)
              }
              onRead={(offer) => setReading({ marketplace: m.name, offer })}
            />
          ))}
        </div>
        <p className="mt-2 text-[12.5px] text-faint">
          {t("plugins.cli")} <Mono>witx plugin marketplace add</Mono>
        </p>
      </section>
      {reading && (
        <Read
          marketplace={reading.marketplace}
          offer={reading.offer}
          installed={installed.find((p) => p.name === reading.offer.name)}
          onClose={() => setReading(undefined)}
          onInstalled={() => {
            setReading(undefined);
            setBrowsing(undefined);
            changed();
          }}
        />
      )}
      {removing && (
        <Remove
          plugin={removing}
          onClose={() => setRemoving(undefined)}
          onRemoved={() => {
            setRemoving(undefined);
            changed();
          }}
        />
      )}
    </div>
  );
}

/** One marketplace: what it offers, fetched when the person opens it. */
function Marketplace({
  name,
  source,
  installed,
  owner,
  open,
  onOpen,
  onRead,
}: {
  name: string;
  source: string;
  installed: InstalledPluginView[];
  owner: boolean;
  open: boolean;
  onOpen: () => void;
  onRead: (offer: PluginOfferView) => void;
}) {
  const { t } = useI18n();
  return (
    <Card className="p-4">
      <div className="flex items-center gap-3">
        <span className="min-w-0 flex-1">
          <span className="text-[13.5px] font-medium">{name}</span>
          <Mono className="block truncate text-[12px] text-faint">
            {source}
          </Mono>
        </span>
        {owner && (
          <Button size="sm" icon={RefreshCw} onClick={onOpen}>
            {open ? t("plugins.close") : t("plugins.browse")}
          </Button>
        )}
      </div>
      {open && (
        <Offers marketplace={name} installed={installed} onRead={onRead} />
      )}
    </Card>
  );
}

function Offers({
  marketplace,
  installed,
  onRead,
}: {
  marketplace: string;
  installed: InstalledPluginView[];
  onRead: (offer: PluginOfferView) => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const offers = useAsync(() => api.offers(marketplace), [api, marketplace]);
  if (offers.error)
    return (
      <p role="alert" className="mt-2 text-[13px] text-red">
        {offers.error.message}
      </p>
    );
  if (!offers.data)
    return (
      <p className="mt-2 text-[13px] text-faint">{t("plugins.fetching")}</p>
    );
  if (offers.data.length === 0)
    return <p className="mt-2 text-[13px] text-faint">{t("plugins.none")}</p>;
  return (
    <ul className="mt-2 divide-y divide-line">
      {offers.data.map((offer) => {
        const mine = installed.find((p) => p.name === offer.name);
        const current =
          mine?.marketplace === marketplace && mine.commit === offer.commit;
        return (
          <li key={offer.name} className="flex items-start gap-3 py-2">
            <span className="min-w-0 flex-1 text-[13px]">
              <Mono className="text-fg">{offer.name}</Mono>
              {offer.version && (
                <span className="text-muted"> v{offer.version}</span>
              )}
              <span className="block text-muted">{offer.description}</span>
              {offer.trouble.map((reason) => (
                <span key={reason} className="block text-amber">
                  {reason}
                </span>
              ))}
            </span>
            {current ? (
              <span className="text-[12.5px] text-faint">
                {t("plugins.upToDate")}
              </span>
            ) : (
              <Button
                size="sm"
                icon={Download}
                disabled={!offer.commit}
                onClick={() => onRead(offer)}
              >
                {mine ? t("plugins.update") : t("plugins.install")}
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Reading a plugin before it is installed: all of it, or what changes. */
function Read({
  marketplace,
  offer,
  installed,
  onClose,
  onInstalled,
}: {
  marketplace: string;
  offer: PluginOfferView;
  installed?: InstalledPluginView;
  onClose: () => void;
  onInstalled: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const fits = offer.trouble.length === 0;

  async function install() {
    setBusy(true);
    setError(undefined);
    try {
      await api.installPlugin(marketplace, offer.name, {
        commit: offer.commit,
        sha256: offer.sha256,
      });
      onInstalled();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      wide
      onClose={onClose}
      title={
        installed
          ? t("plugins.update.title", {
              name: offer.name,
              version: offer.version,
            })
          : t("plugins.install.title", {
              name: offer.name,
              version: offer.version,
            })
      }
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={!fits}
            onClick={() => void install()}
          >
            {installed ? t("plugins.update") : t("plugins.install")}
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-[13.5px]">
        <p>{offer.description}</p>
        <p className="text-muted">
          {t("plugins.from", { marketplace })}{" "}
          <Mono className="break-all">
            {offer.source} @ {offer.commit.slice(0, 12)}
          </Mono>
          {installed &&
            ` · ${t("plugins.was", { version: installed.version })}`}
        </p>
        <p className="rounded-lg bg-amber-soft p-3">{t("plugins.warning")}</p>
        {offer.trouble.map((reason) => (
          <p key={reason} role="alert" className="text-red">
            {reason}
          </p>
        ))}
        {offer.files
          .filter((f) => f.text !== f.installed)
          .map((file) => (
            <div key={file.path}>
              <Mono className="text-[12.5px] text-faint">{file.path}</Mono>
              <pre
                aria-label={file.path}
                className="mt-1 max-h-[50vh] overflow-auto rounded-lg border border-line p-3 font-mono text-[12.5px] leading-relaxed whitespace-pre-wrap"
              >
                {lineDiff(file.installed ?? "", file.text).map((line, i) => (
                  <div
                    key={i}
                    className={cn(
                      line.kind === "added" &&
                        file.installed !== null &&
                        "bg-green-soft text-green",
                      line.kind === "removed" && "bg-red-soft text-red",
                      line.kind === "same" && "text-muted",
                    )}
                  >
                    {file.installed === null
                      ? line.text
                      : `${line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " "} ${line.text}`}
                  </div>
                ))}
              </pre>
            </div>
          ))}
        {offer.removed.map((path) => (
          <p key={path} className="text-red">
            {t("plugins.removedFile", { path })}
          </p>
        ))}
      </div>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-red">
          {error}
        </p>
      )}
    </Dialog>
  );
}

function Remove({
  plugin,
  onClose,
  onRemoved,
}: {
  plugin: InstalledPluginView;
  onClose: () => void;
  onRemoved: () => void;
}) {
  const { t } = useI18n();
  const { api } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  async function remove() {
    setBusy(true);
    setError(undefined);
    try {
      await api.removePlugin(plugin.name);
      onRemoved();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }
  return (
    <Dialog
      open
      tone="red"
      onClose={onClose}
      title={t("plugins.remove.title", { name: plugin.name })}
      footer={
        <>
          <Button data-autofocus onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="danger" busy={busy} onClick={() => void remove()}>
            {t("plugins.remove")}
          </Button>
        </>
      }
    >
      <p className="text-[13.5px]">{t("plugins.remove.help")}</p>
      {error && (
        <p role="alert" className="mt-2 text-[13px] text-red">
          {error}
        </p>
      )}
    </Dialog>
  );
}
