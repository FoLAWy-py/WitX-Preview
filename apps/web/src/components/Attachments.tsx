import { useRef, useState, useSyncExternalStore } from "react";
import { FileText, Paperclip, X } from "lucide-react";
import type { FileView } from "@/api/types.gen";
import { Button } from "@/components/ui/base";
import { useI18n } from "@/i18n";
import { useAuth } from "@/lib/auth";
import { byteSize, fitFiles, MAX_FILE_SIZE, MAX_FILES } from "@/lib/files";
import { newId } from "@/lib/id";
import { read, write } from "@/lib/stored";
import { cn } from "@/lib/format";

interface Picked {
  key: string;
  name: string;
  size: number;
  /** Set once uploaded: what the message sends. */
  id?: string;
  error?: string;
}

// Picked files live outside the message box, by draft: the box is drawn anew as the task
// page loads, and an upload under way finishes into the same list. Uploaded ones are
// kept with the draft, so they are still there after a reload.
const picked = new Map<string, Picked[]>();
const listeners = new Set<() => void>();

function current(draft: string): Picked[] {
  let items = picked.get(draft);
  if (!items) {
    const kept = draft ? read(draft) : undefined;
    items = Array.isArray(kept) ? (kept as Picked[]) : [];
    picked.set(draft, items);
  }
  return items;
}

function change(draft: string, next: (items: Picked[]) => Picked[]): void {
  const items = next(current(draft));
  picked.set(draft, items);
  // With no draft key (no one signed in yet) the list is only kept in memory.
  if (draft)
    write(
      draft,
      items.some((i) => i.id) ? items.filter((i) => i.id) : undefined,
    );
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Files picked to send with the next message, for the message box `draft` (its
 * draft key). Each is uploaded as it is picked, so sending waits only for what is still
 * on its way; it stays the person's own until the message is sent.
 */
export function useAttachments(key: string | undefined) {
  const draft = key ?? "";
  const { api } = useAuth();
  const items = useSyncExternalStore(subscribe, () => current(draft));
  const picker = useRef<HTMLInputElement>(null);
  // What the last pick left out, and why: said under the files until the next pick.
  const [left, setLeft] = useState<{ tooLarge: string[]; over: number }>();
  const update = (key: string, patch: Partial<Picked>) =>
    change(draft, (all) =>
      all.map((i) => (i.key === key ? { ...i, ...patch } : i)),
    );

  function add(files: FileList | null) {
    const fit = fitFiles(Array.from(files ?? []), current(draft).length);
    setLeft(
      fit.tooLarge.length || fit.over
        ? { tooLarge: fit.tooLarge.map((f) => f.name), over: fit.over }
        : undefined,
    );
    for (const file of fit.take) {
      const key = newId();
      change(draft, (all) => [
        ...all,
        { key, name: file.name, size: file.size },
      ]);
      api.uploadFile(file).then(
        (kept) => update(key, { id: kept.id, name: kept.name }),
        (err: unknown) => update(key, { error: (err as Error).message }),
      );
    }
  }

  return {
    items,
    picker,
    add,
    left,
    ids: items.flatMap((i) => (i.id ? [i.id] : [])),
    /** Sending waits until each is uploaded, or removed after it failed. */
    pending: items.some((i) => !i.id),
    full: items.length >= MAX_FILES,
    remove: (key: string) =>
      change(draft, (all) => all.filter((i) => i.key !== key)),
    clear: () => {
      setLeft(undefined);
      change(draft, () => []);
    },
    /** A withdrawn message's files, back to be sent with it again. */
    restore: (files: FileView[]) =>
      change(draft, (all) => [
        ...all,
        ...files.map((f) => ({
          key: f.id,
          id: f.id,
          name: f.name,
          size: f.size,
        })),
      ]),
  };
}

export type Attachments = ReturnType<typeof useAttachments>;

export function AttachButton({
  files,
  disabled,
}: {
  files: Attachments;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  const limit = t("files.limit", { count: MAX_FILES, size: MAX_FILE_SIZE });
  return (
    <>
      <input
        ref={files.picker}
        type="file"
        multiple
        hidden
        aria-label={t("files.attach")}
        onChange={(e) => {
          files.add(e.target.files);
          e.target.value = "";
        }}
      />
      <Button
        variant="ghost"
        iconOnly
        icon={Paperclip}
        aria-label={t("files.attach")}
        aria-description={limit}
        title={
          files.full
            ? t("files.full", { count: MAX_FILES })
            : `${t("files.attach")} · ${limit}`
        }
        disabled={disabled || files.full}
        onClick={() => files.picker.current?.click()}
        className="rounded-full"
      />
    </>
  );
}

/** The files picked for the next message, each removable. */
export function PickedFiles({ files }: { files: Attachments }) {
  const { t } = useI18n();
  const left = files.left;
  if (!files.items.length && !left) return null;
  return (
    <div className="px-3 pb-1.5">
      {left && (
        <p role="alert" className="pb-1 text-[12.5px] text-red">
          {[
            ...left.tooLarge.map((name) =>
              t("files.tooLarge", { name, size: MAX_FILE_SIZE }),
            ),
            ...(left.over
              ? [t("files.tooMany", { count: MAX_FILES, left: left.over })]
              : []),
          ].join(" ")}
        </p>
      )}
      <ul aria-label={t("files.picked")} className="flex flex-wrap gap-1.5">
        {files.items.map((f) => (
          <li
            key={f.key}
            className={cn(
              "flex max-w-full items-center gap-1.5 rounded-lg border border-line bg-sunken py-1 pr-1 pl-2 text-[13px]",
              f.error && "border-red/50 text-red",
            )}
          >
            <FileText size={14} aria-hidden className="shrink-0 text-muted" />
            <span className="min-w-0 truncate">{f.name}</span>
            <span className="shrink-0 text-faint" title={f.error}>
              {f.error
                ? t("files.failed")
                : f.id
                  ? byteSize(f.size)
                  : t("files.uploading")}
            </span>
            <button
              type="button"
              aria-label={t("files.remove", { name: f.name })}
              onClick={() => files.remove(f.key)}
              className="shrink-0 cursor-pointer rounded p-0.5 text-faint hover:text-fg"
            >
              <X size={13} aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      <p className="pt-1 text-[12px] text-faint">
        {t("files.limit", { count: MAX_FILES, size: MAX_FILE_SIZE })}
      </p>
    </div>
  );
}

/** The files that came with a message, in the conversation. */
export function SentFiles({ files }: { files: FileView[] }) {
  const { t } = useI18n();
  if (!files.length) return null;
  return (
    <ul aria-label={t("files.sent")} className="mt-1.5 space-y-1">
      {files.map((f) => (
        <li
          key={f.id}
          className="flex items-center gap-1.5 text-[13px] text-muted"
        >
          <FileText size={14} aria-hidden className="shrink-0" />
          <span className="min-w-0 truncate text-fg">{f.name}</span>
          <span className="shrink-0">{byteSize(f.size)}</span>
        </li>
      ))}
    </ul>
  );
}
