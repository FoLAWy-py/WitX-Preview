import {
  Fragment,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  ArrowLeft,
  ChevronDown,
  ChevronUp,
  Hand,
  House,
  Keyboard,
  Lock,
  MousePointer2,
  Move,
  Pause,
  Play,
  Send,
  ShieldCheck,
  Smartphone,
  Timer,
  Volume1,
  Volume2,
  X,
  type LucideIcon,
} from "lucide-react";
import type { DeviceView, TakeoverView } from "@/api/types.gen";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/format";
import { newId } from "@/lib/id";
import { useLayout } from "@/lib/prefs";
import { fitScreen } from "@/lib/screen";
import {
  Frame,
  KEY_SHORTCUT,
  keyState,
  SIDE_KEYS,
  SideKey,
  StatusChip,
  Toast,
  useBoxSize,
  why,
  type Mark,
  type Recent,
  type SideKind,
} from "./takeover/parts";
import { useScreen, type Point } from "./takeover/screen";
import { useTakeoverSocket } from "./takeover/socket";
import { Button, Ico, inputClass } from "./ui/base";
import { Menu } from "./ui/popover";

/** A picture older than this says so: the phone sends one at least every second,
 * so a longer wait means the network or the phone is slow, and input would land on a
 * screen the person has not seen. */
const DELAYED_MS = 3_000;
/** Inputs kept in the list of recent ones. */
const RECENT = 6;
/** The shape of a phone's screen, for the frame shown before the first picture. */
const PLACEHOLDER = { width: 1080, height: 2340 };

type Mode = "inline" | "phone" | "phoneSide" | "wide";

/** Why a takeover ended, in words; unknown reasons are shown as sent. */
const ENDED: Record<string, MessageKey> = {
  disconnected: "takeover.ended.disconnected",
  timeout: "takeover.ended.timeout",
  ended_elsewhere: "takeover.ended.elsewhere",
  not_active: "takeover.ended.notActive",
  device_not_responding: "takeover.ended.notResponding",
  device_locked: "takeover.ended.locked",
};

/** Product names, as written: not translated. */
const PLATFORM: Record<string, string> = { android: "Android", ios: "iOS" };
const KEY_ICON: Record<SideKind, LucideIcon> = {
  volume_up: Volume2,
  volume_down: Volume1,
  lock: Lock,
};

/**
 * Live control of a device: its screen, refreshed many times a second,
 * where a click is a tap, a held click a long press and a drag a swipe. A drag is sent
 * with the way the pointer took, and that way is drawn on the picture as it is
 * made, until the next picture arrives. The phone is drawn in its frame, with the side
 * keys it has where they are on the phone (volume, lock). Typed text goes to the device
 * only; it is not saved anywhere, and the list of recent inputs says only how much was
 * typed. Ending hands the device back to the agent or keeps the task paused.
 *
 * `inline` puts it in the task page's right panel, next to the conversation; otherwise
 * it covers the screen: on a phone with its controls where a thumb reaches them, on a
 * phone held sideways with them in a column beside the picture, and on a tablet or a
 * computer with them beside it. `outsideTask`: taken over from the device pool, so there
 * is no agent to hand back to; ending frees the phone. `onAgain` takes the phone over
 * once more after a takeover ended.
 */
export function Takeover({
  takeover,
  device,
  inline,
  outsideTask,
  onClosed,
  onAgain,
}: {
  takeover: TakeoverView;
  device: DeviceView | undefined;
  inline?: boolean;
  outsideTask?: boolean;
  onClosed: () => void;
  onAgain?: () => void;
}) {
  const { t } = useI18n();
  const { layout, coarse, landscapePhone } = useLayout();
  const seq = useRef(0);
  const image = useRef<HTMLCanvasElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const room = useRef<HTMLDivElement>(null);
  const roomSize = useBoxSize(room);
  const [ended, setEnded] = useState<string>();
  const [note, setNote] = useState<string>();
  const [text, setText] = useState("");
  const [typing, setTyping] = useState(false);
  const [confirmLock, setConfirmLock] = useState(false);
  const [showRecent, setShowRecent] = useState(false);
  const [marks, setMarks] = useState<Mark[]>([]);
  const [recent, setRecent] = useState<Recent[]>([]);
  // The person locked the phone: the takeover ends because of it, and says so.
  const locked = useRef(false);
  // How long the takeover may sit idle, as the server set it, and when it last had input.
  const [idleMs] = useState(() =>
    Math.max(30_000, Date.parse(takeover.expires_at) - Date.now()),
  );
  const lastActive = useRef(Date.now());
  const [now, setNow] = useState(() => Date.now());
  const name = device?.name ?? t("common.device");
  const mode: Mode = inline
    ? "inline"
    : layout === "phone"
      ? landscapePhone
        ? "phoneSide"
        : "phone"
      : "wide";
  const can = (kind: string) => device?.capabilities.includes(kind as never);
  const sideKeys = (["volume_up", "volume_down", "lock"] as const).filter(can);

  const picture = useScreen({
    canvas: image,
    ended: ended !== undefined,
    send,
    pressed: mark,
    turned: () =>
      setNote(t("takeover.notDone", { why: t("takeover.why.turned") })),
  });
  const { frame, shown, lastPicture } = picture;
  const { onPointerDown, onPointerMove, onPointerUp } = picture.pointer;

  const socket = useTakeoverSocket(takeover.takeover_id, {
    frame: (message) => void picture.show(message),
    result: (message) => {
      const state: Recent["state"] =
        message.outcome === "returned"
          ? "done"
          : message.outcome === "unknown"
            ? "unknown"
            : "failed";
      setMarks((all) =>
        all.map((m) =>
          m.input === message.input_id
            ? { ...m, state: state === "done" ? "done" : "failed" }
            : m,
        ),
      );
      setRecent((all) =>
        all.map((r) => {
          if (r.id !== message.input_id) return r;
          if (r.kind === "lock" && state === "done") locked.current = true;
          return {
            ...r,
            state,
            why: state === "done" ? undefined : why(message, t),
          };
        }),
      );
      setNote(state === "done" ? undefined : why(message, t));
    },
    ended: setEnded,
    closed: () => setEnded((reason) => reason ?? "disconnected"),
  });

  useEffect(() => {
    if (inline) return;
    document.body.style.overflow = "hidden";
    root.current?.focus({ preventScroll: true });
    return () => {
      document.body.style.overflow = "";
    };
  }, [inline]);

  // Ended by you: close. Ended for another reason: stay, and say why.
  useEffect(() => {
    if (ended === "ended") onClosed();
  }, [ended, onClosed]);

  // A clock for the idle warning and the picture's delay.
  useEffect(() => {
    if (ended) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ended]);

  function send(input: Record<string, unknown>, frameId: number): string {
    seq.current += 1;
    const id = newId();
    lastActive.current = Date.now();
    socket.send({
      type: "input",
      input_id: id,
      client_seq: seq.current,
      frame_id: frameId,
      input,
    });
    // The kind, and for typing only how much: what was typed is not kept, even here.
    const kind = String(input.kind);
    const chars = kind === "type_text" ? String(input.text).length : undefined;
    setRecent((all) => [
      { id, kind, chars, at: Date.now(), state: "sent" },
      ...all.slice(0, RECENT - 1),
    ]);
    return id;
  }

  /** A key of the phone: its system buttons and its side keys. */
  function key(kind: "back" | "home" | SideKind) {
    const on = shown.current;
    if (!on || ended) return;
    if (kind === "lock" && !confirmLock) {
      setConfirmLock(true); // locking ends the takeover: asked first
      return;
    }
    setConfirmLock(false);
    send({ kind }, on.frame_id);
  }

  /** Shows at once where a press landed; its result colours it, then it goes. Kept by
   * a timer, not an animation event, so it also goes with reduced motion. */
  function mark(at: Point, input: string) {
    const el = image.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    const id = newId();
    setMarks((all) => [
      ...all.slice(-2),
      { id, input, x: at.x - box.left, y: at.y - box.top, state: "sent" },
    ]);
    setTimeout(() => setMarks((all) => all.filter((m) => m.id !== id)), 1600);
  }

  function typeText(event: FormEvent) {
    event.preventDefault();
    const on = shown.current;
    if (!text || !on) return;
    send({ kind: "type_text", text }, on.frame_id);
    setText("");
  }

  function keep() {
    lastActive.current = Date.now();
    socket.send({ type: "keep" });
  }

  function finish(resume: boolean) {
    socket.send({ type: "end", resume });
  }

  /** The side keys from the keyboard, anywhere in the takeover but a text field:
   * Alt+↑, Alt+↓ and Alt+L (the lock still asks first). Escape closes that question. */
  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape" && confirmLock) {
      setConfirmLock(false);
      return;
    }
    const field = (event.target as HTMLElement).closest("input, textarea");
    if (field || !event.altKey) return;
    const kind: SideKind | undefined =
      event.key === "ArrowUp"
        ? "volume_up"
        : event.key === "ArrowDown"
          ? "volume_down"
          : event.code === "KeyL"
            ? "lock"
            : undefined;
    if (!kind || !can(kind)) return;
    event.preventDefault();
    key(kind);
  }

  const live = frame !== undefined && !ended;
  const left = Math.ceil((lastActive.current + idleMs - now) / 1000);
  const warn = live && left <= 30;
  const late = live ? Math.floor((now - lastPicture.current) / 1000) : 0;
  const delayed = live && now - lastPicture.current > DELAYED_MS;
  const endedWords = !ended
    ? undefined
    : ended === "device_locked" && locked.current
      ? t("takeover.ended.lockedByYou")
      : ENDED[ended]
        ? t(ENDED[ended])
        : t("takeover.ended", { reason: ended.replace(/_/g, " ") });

  // ---------------------------------------------------------------- the phone

  // Side keys go on the phone's edges when it stands upright and there is room; on a
  // phone held sideways, or a picture on its side, they join the other buttons.
  const keysOnFrame =
    sideKeys.length > 0 &&
    mode !== "phoneSide" &&
    !(frame && frame.width > frame.height);
  const fit = fitScreen(frame ?? PLACEHOLDER, roomSize, keysOnFrame);

  const screen = (
    <div
      className="relative shrink-0"
      style={{
        width: fit.width + 2 * fit.bezel,
        height: fit.height + 2 * fit.bezel,
      }}
    >
      {keysOnFrame &&
        SIDE_KEYS[device?.platform === "ios" ? "ios" : "android"]
          .filter((spec) => can(spec.kind))
          .map((spec) => (
            <SideKey
              key={spec.kind}
              spec={spec}
              fit={fit}
              coarse={coarse}
              label={t(`takeover.key.${spec.kind}`)}
              state={keyState(recent, spec.kind, now)}
              disabled={!live}
              onPress={() => key(spec.kind)}
            />
          ))}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          borderRadius: fit.radius + fit.bezel,
          background: "#0d0e10",
          boxShadow:
            "inset 0 0 0 1.5px #3a3d44, inset 0 0 0 3px #16171a, 0 24px 48px -20px rgb(20 18 15 / 0.45), 0 2px 8px rgb(20 18 15 / 0.12)",
        }}
      />
      <div
        className="absolute overflow-hidden bg-black"
        style={{
          left: fit.bezel,
          top: fit.bezel,
          width: fit.width,
          height: fit.height,
          borderRadius: fit.radius,
        }}
      >
        {frame ? (
          <canvas
            ref={image}
            role="img"
            width={frame.width}
            height={frame.height}
            aria-label={t("takeover.screen", { device: name })}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            style={{ width: fit.width, height: fit.height }}
            className={cn(
              "block touch-none select-none",
              live ? "cursor-crosshair" : "opacity-40",
            )}
          />
        ) : (
          <div className="flex h-full flex-col gap-3 bg-[#1c1d21] p-[8%] pt-[18%]">
            <div className="h-[4%] w-1/2 rounded-md bg-[#2a2c31]" />
            <div className="h-[11%] rounded-xl bg-[#24262b]" />
            <div className="h-[11%] rounded-xl bg-[#24262b]" />
            <div className="h-[11%] rounded-xl bg-[#24262b]" />
            <p className="flex flex-1 items-center justify-center text-center text-[13px] text-[#8b8e96]">
              {ended ? "" : t("takeover.waiting")}
            </p>
          </div>
        )}
        {marks.map((m) => (
          <span
            key={m.id}
            aria-hidden
            style={{ left: m.x, top: m.y }}
            className="pointer-events-none absolute"
          >
            <span
              className={cn(
                "anim-ripple absolute -top-6 -left-6 h-12 w-12 rounded-full",
                m.state === "failed" ? "bg-amber/30" : "bg-accent/30",
              )}
            />
            <span
              className={cn(
                "absolute -top-1.5 -left-1.5 h-3 w-3 rounded-full ring-2 ring-white transition-colors",
                m.state === "failed" ? "bg-amber" : "bg-accent",
              )}
            />
          </span>
        ))}
      </div>
    </div>
  );

  // ---------------------------------------------------------------- what is said over it

  // At the foot of the phone's room: the side keys are on the upper half of its frame
  // (SIDE_KEYS), and a warning at the top came up over the key someone was about to
  // press.
  const toasts = (
    <div className="pointer-events-none absolute inset-x-3 bottom-3 z-10 flex flex-col items-center gap-2">
      {delayed && !warn && (
        <Toast icon={AlertCircle}>
          {t("takeover.delayed.note", { n: late })}
        </Toast>
      )}
      {warn && (
        <Toast
          icon={Timer}
          action={
            <Button size="sm" onClick={keep}>
              {t("takeover.keep")}
            </Button>
          }
        >
          {t("takeover.idle", { n: Math.max(0, left) })}
        </Toast>
      )}
      {note && !ended && (
        <Toast
          icon={AlertCircle}
          action={
            <Button
              size="sm"
              variant="ghost"
              icon={X}
              iconOnly
              aria-label={t("takeover.dismiss")}
              onClick={() => setNote(undefined)}
            />
          }
        >
          {note}
        </Toast>
      )}
    </div>
  );

  const lockQuestion = confirmLock && !ended && (
    <div
      role="alertdialog"
      aria-labelledby="takeover-lock-title"
      aria-describedby="takeover-lock-body"
      className="elev anim-pop absolute inset-x-4 top-1/2 z-20 mx-auto max-w-[300px] -translate-y-1/2 space-y-2 rounded-xl border border-line bg-surface p-4"
    >
      <p
        id="takeover-lock-title"
        className="flex items-center gap-2 text-[14.5px] font-semibold"
      >
        <Ico icon={Lock} size={16} className="text-muted" />
        {t("takeover.lock.title")}
      </p>
      <p id="takeover-lock-body" className="text-[13px] text-muted">
        {t("takeover.lock.body")}
      </p>
      <div className="flex justify-end gap-2 pt-1">
        <Button size="sm" autoFocus onClick={() => setConfirmLock(false)}>
          {t("common.cancel")}
        </Button>
        <Button size="sm" variant="danger-soft" onClick={() => key("lock")}>
          {t("takeover.key.lock")}
        </Button>
      </div>
    </div>
  );

  const endedCard = ended && (
    <div
      role="alert"
      className="elev anim-pop absolute inset-x-4 top-1/2 z-20 mx-auto max-w-[340px] -translate-y-1/2 space-y-2 rounded-xl border border-line bg-surface p-4"
    >
      <p className="text-[15px] font-semibold">{t("takeover.ended.title")}</p>
      <p className="text-[13.5px]">{endedWords}</p>
      <p className="text-[13px] text-muted">
        {t(outsideTask ? "takeover.ended.free" : "takeover.ended.next")}
      </p>
      <div className="flex flex-wrap justify-end gap-2 pt-1">
        <Button size="sm" icon={X} onClick={onClosed}>
          {t("takeover.close")}
        </Button>
        {onAgain && (
          <Button size="sm" variant="primary" icon={Hand} onClick={onAgain}>
            {t("takeover.again")}
          </Button>
        )}
      </div>
    </div>
  );

  /** Where the phone is drawn: its room is measured, and the phone fills it. */
  const stage = (below?: ReactNode, className?: string) => (
    <div
      className={cn(
        "relative flex min-h-0 min-w-0 flex-1 flex-col items-center bg-sunken",
        className,
      )}
    >
      <div
        ref={room}
        className="relative flex min-h-0 w-full flex-1 items-center justify-center"
      >
        {screen}
        {toasts}
      </div>
      {below}
      {lockQuestion}
      {endedCard}
    </div>
  );

  // ---------------------------------------------------------------- controls

  const status = (
    <StatusChip
      tone={!frame ? "connecting" : delayed ? "delayed" : "live"}
      label={
        !frame
          ? t("takeover.connecting")
          : delayed
            ? t("takeover.delayed")
            : t("takeover.live")
      }
    />
  );

  const keyButtons: {
    kind: "back" | "home" | SideKind;
    icon: LucideIcon;
    label: string;
  }[] = [
    ...(can("back")
      ? [{ kind: "back" as const, icon: ArrowLeft, label: t("takeover.back") }]
      : []),
    { kind: "home" as const, icon: House, label: t("takeover.home") },
  ];
  const sideButtons = keysOnFrame
    ? []
    : sideKeys.map((kind) => ({
        kind,
        icon: KEY_ICON[kind],
        label: t(`takeover.key.${kind}`),
      }));

  /** Back and home under the phone, as one control; the side keys too when they are not
   * on the phone's edges. */
  const toolbar = (
    <div
      role="toolbar"
      aria-label={t("takeover.keys")}
      className="my-3 inline-flex shrink-0 items-center gap-0.5 rounded-xl border border-line bg-surface p-1 shadow-[0_1px_2px_rgb(20_18_15/0.06)]"
    >
      {keyButtons.map((b) => (
        <Button
          key={b.kind}
          size="sm"
          variant="ghost"
          icon={b.icon}
          disabled={!live}
          onClick={() => key(b.kind)}
          className="text-fg"
        >
          {b.label}
        </Button>
      ))}
      {sideButtons.length > 0 && (
        <span aria-hidden className="mx-1 h-5 w-px bg-line" />
      )}
      {sideButtons.map((b) => (
        <Button
          key={b.kind}
          size="sm"
          variant="ghost"
          icon={b.icon}
          iconOnly
          aria-label={b.label}
          title={`${b.label} (${KEY_SHORTCUT[b.kind]})`}
          disabled={!live}
          onClick={() => key(b.kind)}
          className="text-fg"
        />
      ))}
    </div>
  );

  const how: [MessageKey, MessageKey][] = [
    [coarse ? "takeover.how.touch" : "takeover.how.click", "takeover.does.tap"],
    ["takeover.how.hold", "takeover.does.longPress"],
    ["takeover.how.drag", "takeover.does.swipe"],
    ...(coarse
      ? []
      : [
          ["takeover.how.wheel", "takeover.does.scroll"] as [
            MessageKey,
            MessageKey,
          ],
        ]),
    ...(keysOnFrame && mode !== "phone"
      ? [
          ["takeover.how.keys", "takeover.does.keys"] as [
            MessageKey,
            MessageKey,
          ],
        ]
      : []),
  ];
  const hint = (className?: string) => (
    <p
      className={cn(
        "text-center text-[12px] text-balance text-muted",
        className,
      )}
    >
      {how.map(([verb, does], i) => (
        <Fragment key={verb}>
          {i > 0 && <span aria-hidden> · </span>}
          <span className="whitespace-nowrap">
            <b className="font-medium text-fg">{t(verb)}</b> {t(does)}
          </span>
        </Fragment>
      ))}
    </p>
  );

  /** `compact`: no visible label, and the field takes focus (a phone, after "Type");
   * `quiet`: the note on privacy shows while typing only (the narrow panel). */
  const typeForm = (compact?: boolean, quiet?: boolean) => (
    <form onSubmit={typeText} className="group space-y-2">
      <label
        htmlFor="takeover-text"
        className={cn("text-[13px] font-medium", compact && "sr-only")}
      >
        {t("takeover.type")}
      </label>
      <div className="flex gap-2">
        <input
          id="takeover-text"
          autoComplete="off"
          spellCheck={false}
          autoFocus={compact}
          value={text}
          disabled={!!ended}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("takeover.type.placeholder")}
          className={inputClass}
        />
        <Button
          variant="primary"
          type="submit"
          icon={Send}
          iconOnly
          aria-label={t("takeover.send")}
          disabled={!text || !live}
        />
      </div>
      <p
        className={cn(
          "items-start gap-2 text-[12px] text-muted",
          quiet && !text ? "hidden group-focus-within:flex" : "flex",
        )}
      >
        <Ico icon={ShieldCheck} size={14} className="mt-0.5 text-faint" />
        {t("takeover.privacy")}
      </p>
    </form>
  );

  const recentList = (
    <ol className="text-[13px]">
      {recent.length === 0 && (
        <li className="py-2 text-muted">{t("takeover.recent.empty")}</li>
      )}
      {recent.map((r) => (
        <li
          key={r.id}
          title={r.why}
          className="flex items-center gap-2.5 border-b border-line/70 py-2 last:border-b-0"
        >
          <span
            aria-hidden
            className={cn(
              "h-2 w-2 shrink-0 rounded-full",
              r.state === "sent"
                ? "bg-accent"
                : r.state === "done"
                  ? "bg-faint"
                  : "bg-amber",
            )}
          />
          <span className="min-w-0 flex-1 truncate">
            {r.chars !== undefined
              ? t("takeover.op.typed", { count: r.chars })
              : t(`kind.${r.kind}` as MessageKey)}
          </span>
          <span className="shrink-0 text-[12px] text-muted">
            {t(`takeover.op.${r.state}`)}
          </span>
        </li>
      ))}
    </ol>
  );

  // One above the other: both say what they do in full, and a narrow panel has no
  // room for them side by side.
  const endButtons = () =>
    outsideTask ? (
      <Button variant="primary" onClick={() => finish(false)}>
        {t("takeover.finish")}
      </Button>
    ) : (
      <div className="grid gap-2 @[29rem]:grid-cols-2">
        <Button variant="primary" icon={Play} onClick={() => finish(true)}>
          {t("takeover.handBack")}
        </Button>
        <Button icon={Pause} onClick={() => finish(false)}>
          {t("takeover.keepPaused")}
        </Button>
      </div>
    );

  const endMenu = (full?: boolean) =>
    outsideTask ? (
      <Button className={cn(full && "w-full")} onClick={() => finish(false)}>
        {t("takeover.end")}
      </Button>
    ) : (
      <Menu
        label={t("takeover.end")}
        placement={full ? "top-end" : "bottom-end"}
        entries={[
          {
            id: "resume",
            label: t("takeover.handBack"),
            icon: Play,
            onSelect: () => finish(true),
          },
          {
            id: "pause",
            label: t("takeover.keepPaused"),
            icon: Pause,
            onSelect: () => finish(false),
          },
        ]}
        trigger={({ ref, open, toggle, onKeyDown: menuKeys }) => (
          <Button
            ref={ref}
            aria-haspopup="menu"
            aria-expanded={open}
            onClick={toggle}
            onKeyDown={menuKeys}
            className={cn(full && "w-full")}
          >
            {t("takeover.end")}
            <Ico icon={full ? ChevronUp : ChevronDown} size={15} />
          </Button>
        )}
      />
    );

  // Leaving ends a running takeover first (the phone is freed, or its task stays
  // paused). Ended or not yet connected: close; closing the socket ends it too.
  const leave = () => (!ended && socket.open() ? finish(false) : onClosed());
  const leaveButton = (
    <Button
      variant="ghost"
      icon={ArrowLeft}
      iconOnly
      onClick={leave}
      title={ended ? undefined : t("takeover.leave.live")}
      aria-label={ended ? t("takeover.leave") : t("takeover.leave.live")}
      className="-ml-1.5 text-fg"
    />
  );

  /** Who is in control, of which phone, and whether the picture is live. A plain bar:
   * the strong colour is kept for what to do, not for what is going on. */
  const header = (children?: ReactNode, back = true) => (
    <div className="flex min-h-12 shrink-0 items-center gap-2.5 border-b border-line bg-surface px-3 py-1.5 md:px-4">
      {back && leaveButton}
      {mode !== "phoneSide" && (
        <Ico
          icon={Hand}
          size={17}
          className={ended ? "text-muted" : "text-fg"}
        />
      )}
      <span className="flex min-w-0 flex-1 flex-col leading-tight">
        <strong className={cn("truncate text-[14.5px]", ended && "text-muted")}>
          {ended ? t("takeover.ended.title") : t("takeover.banner")}
        </strong>
        <span className="truncate text-[12.5px] text-muted">{name}</span>
      </span>
      {!ended && status}
      {children}
    </div>
  );

  const label = t("takeover.title", { device: name });

  if (mode === "inline") {
    return (
      <Frame label={label} inline rootRef={root} onKeyDown={onKeyDown}>
        {header(undefined, false)}
        {stage(
          <>
            {toolbar}
            {hint("px-4 pb-3")}
          </>,
          "min-h-[360px]",
        )}
        <div className="space-y-3 border-t border-line p-4">
          {typeForm(false, true)}
        </div>
        <div className="border-t border-line">
          <button
            type="button"
            aria-expanded={showRecent}
            onClick={() => setShowRecent((v) => !v)}
            className="flex min-h-11 w-full items-center gap-2 px-4 text-left text-[13px] hover:bg-hovered"
          >
            <span className="font-medium">{t("takeover.recent")}</span>
            <span className="min-w-0 flex-1 truncate text-right text-muted">
              {recent[0] &&
                `${
                  recent[0].chars !== undefined
                    ? t("takeover.op.typed", { count: recent[0].chars })
                    : t(`kind.${recent[0].kind}` as MessageKey)
                } · ${t(`takeover.op.${recent[0].state}`)}`}
            </span>
            <Ico
              icon={showRecent ? ChevronUp : ChevronDown}
              size={16}
              className="text-muted"
            />
          </button>
          {showRecent && <div className="px-4 pb-2">{recentList}</div>}
        </div>
        {!ended && (
          <div className="@container sticky bottom-0 space-y-2.5 border-t border-line bg-surface p-4">
            <p className="text-[12.5px] text-muted">
              {t("takeover.handBack.note")}
            </p>
            {endButtons()}
          </div>
        )}
      </Frame>
    );
  }

  if (mode === "phone") {
    // A phone: the picture as large as it fits, and what a thumb needs at the bottom,
    // like the phone's own navigation bar. Ending, done once and with consequences,
    // sits at the top, away from the thumb.
    const dock = [
      ...keyButtons.map((b) => ({
        ...b,
        onClick: () => key(b.kind),
        pressed: undefined,
      })),
      {
        kind: "type" as const,
        icon: Keyboard,
        label: t("takeover.keyboard"),
        onClick: () => setTyping((v) => !v),
        pressed: typing,
      },
    ];
    return createPortal(
      <Frame label={label} rootRef={root} onKeyDown={onKeyDown}>
        {header(!ended && endMenu())}
        {stage(
          // How to use it, until it has been used.
          recent.length === 0 && !note && !warn && !delayed && !ended && (
            <div className="pointer-events-none absolute inset-x-0 top-3 flex justify-center px-3">
              {hint(
                "elev rounded-2xl border border-line bg-surface px-3 py-1.5",
              )}
            </div>
          ),
        )}
        {typing && !ended && (
          <div className="border-t border-line bg-surface px-3 pt-3 pb-1">
            {typeForm(true)}
          </div>
        )}
        <div
          role="toolbar"
          aria-label={t("takeover.keys")}
          className={cn(
            "grid shrink-0 gap-1 border-t border-line bg-surface px-2 pt-1.5 pb-[max(0.5rem,env(safe-area-inset-bottom))]",
            dock.length + sideButtons.length > 3
              ? "grid-cols-4"
              : "grid-cols-3",
          )}
        >
          {[
            ...dock,
            ...sideButtons.map((b) => ({
              ...b,
              onClick: () => key(b.kind),
              pressed: undefined,
            })),
          ].map((b) => (
            <button
              key={b.kind}
              type="button"
              aria-pressed={b.pressed}
              disabled={b.kind === "type" ? !!ended : !live}
              onClick={b.onClick}
              className={cn(
                "press flex h-14 flex-col items-center justify-center gap-0.5 rounded-lg text-[12px] font-medium disabled:opacity-40",
                b.pressed ? "bg-selected text-fg" : "text-fg hover:bg-hovered",
              )}
            >
              <Ico icon={b.icon} size={20} />
              {b.label}
            </button>
          ))}
        </div>
      </Frame>,
      document.body,
    );
  }

  if (mode === "phoneSide") {
    // A phone on its side: the picture uses the whole height; the controls stand in a
    // column beside it, the side keys among them (they would be too small on its edge).
    return createPortal(
      <Frame label={label} rootRef={root} onKeyDown={onKeyDown} row>
        {stage(hint("pb-2 px-3"))}
        <div className="flex w-[284px] shrink-0 flex-col overflow-y-auto border-l border-line bg-surface pb-[max(0.75rem,env(safe-area-inset-bottom))]">
          {header()}
          <div
            role="toolbar"
            aria-label={t("takeover.keys")}
            className="grid grid-cols-3 gap-1.5 p-3"
          >
            {[
              ...keyButtons.map((b) => ({
                ...b,
                pressed: undefined as boolean | undefined,
              })),
              {
                kind: "type" as const,
                icon: Keyboard,
                label: t("takeover.keyboard"),
                pressed: typing as boolean | undefined,
              },
              ...sideButtons.map((b) => ({
                ...b,
                pressed: undefined as boolean | undefined,
              })),
            ].map((b) => (
              <button
                key={b.kind}
                type="button"
                aria-pressed={b.pressed}
                disabled={b.kind === "type" ? !!ended : !live}
                onClick={() =>
                  b.kind === "type" ? setTyping((v) => !v) : key(b.kind)
                }
                className={cn(
                  "press flex h-12 flex-col items-center justify-center gap-0.5 rounded-lg border border-line text-[11.5px] font-medium disabled:opacity-40",
                  b.pressed ? "bg-selected" : "hover:bg-hovered",
                )}
              >
                <Ico icon={b.icon} size={17} />
                {b.label}
              </button>
            ))}
          </div>
          {typing && !ended && (
            <div className="px-3 pb-3">{typeForm(true)}</div>
          )}
          <div className="flex-1" />
          {!ended && <div className="px-3">{endMenu(true)}</div>}
        </div>
      </Frame>,
      document.body,
    );
  }

  // A tablet or a computer: the phone in the middle, what to do with it beside it, and
  // on a wide screen how to control it on the other side.
  return createPortal(
    <Frame label={label} rootRef={root} onKeyDown={onKeyDown}>
      {header(
        <>
          {!ended && (
            <span className="hidden items-center gap-1.5 text-[12.5px] text-muted lg:inline-flex">
              <Ico icon={Timer} size={14} />
              {t("takeover.idleAfter", {
                n: Math.max(1, Math.round(idleMs / 60_000)),
              })}
            </span>
          )}
          {outsideTask && !ended && (
            <Button icon={X} onClick={() => finish(false)}>
              {t("takeover.finish")}
            </Button>
          )}
        </>,
      )}
      <div className="flex min-h-0 flex-1 justify-center gap-6 overflow-hidden p-4 md:p-6">
        <aside className="hidden w-[272px] shrink-0 flex-col gap-4 overflow-y-auto xl:flex">
          <section className="space-y-3 rounded-xl border border-line bg-surface p-4">
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-sunken">
                <Ico icon={Smartphone} size={19} className="text-muted" />
              </span>
              <span className="min-w-0 leading-tight">
                <b className="block truncate text-[15px] font-semibold">
                  {name}
                </b>
                <span className="text-[12.5px] text-muted">
                  {PLATFORM[device?.platform ?? ""] ?? device?.platform}
                </span>
              </span>
            </div>
            <p className="text-[13px] text-muted">
              {t(outsideTask ? "takeover.poolFree" : "takeover.agentPaused")}
            </p>
          </section>
          <section
            aria-labelledby="takeover-how"
            className="space-y-3 rounded-xl border border-line bg-surface p-4"
          >
            <h2 id="takeover-how" className="text-[13.5px] font-semibold">
              {t("takeover.how")}
            </h2>
            <dl className="grid grid-cols-[1.25rem_auto_minmax(0,1fr)] items-center gap-x-2.5 gap-y-3 text-[13.5px]">
              {how.map(([verb, does]) => (
                <div key={verb} className="contents">
                  <Ico
                    icon={HOW_ICON[verb] ?? MousePointer2}
                    size={16}
                    className="text-muted"
                  />
                  <dt>{t(verb)}</dt>
                  <dd className="text-right text-muted">{t(does)}</dd>
                </div>
              ))}
            </dl>
          </section>
          <p className="flex gap-2 px-1 text-[12.5px] text-muted">
            <Ico icon={AlertCircle} size={15} className="mt-0.5 text-faint" />
            {t("takeover.automation")}
          </p>
        </aside>

        {stage(
          <>
            {toolbar}
            {hint("px-4 pb-3 xl:hidden")}
          </>,
          "max-w-[600px] rounded-2xl",
        )}

        <aside className="flex w-[300px] shrink-0 flex-col gap-4 overflow-y-auto lg:w-[320px]">
          <p className="text-[13px] text-muted xl:hidden">
            {t(outsideTask ? "takeover.poolFree" : "takeover.agentPaused")}
          </p>
          <section className="rounded-xl border border-line bg-surface p-4">
            {typeForm()}
          </section>
          <section
            aria-labelledby="takeover-recent"
            className="rounded-xl border border-line bg-surface px-4 pt-3.5 pb-1.5"
          >
            <h2
              id="takeover-recent"
              className="mb-1 text-[13.5px] font-semibold"
            >
              {t("takeover.recent")}
            </h2>
            {recentList}
          </section>
          <div className="flex-1" />
          {!outsideTask && !ended && endButtons()}
        </aside>
      </div>
    </Frame>,
    document.body,
  );
}

const HOW_ICON: Partial<Record<MessageKey, LucideIcon>> = {
  "takeover.how.click": MousePointer2,
  "takeover.how.touch": MousePointer2,
  "takeover.how.hold": Timer,
  "takeover.how.drag": Move,
  "takeover.how.wheel": ChevronDown,
  "takeover.how.keys": Volume2,
};
