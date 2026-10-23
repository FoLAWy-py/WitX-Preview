"""Safety rules. Each function decides one question from plain data and never does I/O.

These are the invariants the rest of the system relies on; tests in
``tests/cloud/test_rules.py`` pin them down.
"""

import calendar
import hashlib
import json
import re
from bisect import bisect_left
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from witx.domain.model import (
    ALL_INTENT_FIELDS,
    AUTO_ASK_EFFECTS,
    CHANGE_EFFECT,
    EFFECT_DEFINITIONS,
    INTENT_FIELDS,
    MONEY_FLAGS,
    OPEN_ACTION,
    PAYMENT_EFFECTS,
    SENSITIVE_EFFECTS,
    SHOWN_INTENT_FIELDS,
    TERMINAL_TASK,
    Action,
    ActionStatus,
    Approval,
    Device,
    Effect,
    Grant,
    Lease,
    Observation,
    ObservationStatus,
    PermissionMode,
    PermissionRequest,
    Policy,
    PurchaseIntent,
    PurchaseOption,
    Repeat,
    RiskVerdict,
    Run,
    RunStatus,
    TaskStatus,
    WaitReason,
)


class Rejected(Exception):
    """A request that the rules refuse. ``code`` is stable and safe to show to the model."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def action_digest(
    action_id: UUID,
    device_id: UUID,
    epoch: int,
    payload: Mapping[str, Any],
    intent: Mapping[str, str] | None = None,
) -> str:
    """Binds an action ID to its device, epoch, complete input parameters and, for a
    message or payment, its terms: an approval covers exactly these."""
    body = {"action_id": str(action_id), "device_id": str(device_id), "epoch": epoch, **payload}
    if intent:
        body["intent"] = dict(intent)
    return hashlib.sha256(canonical_json(body)).hexdigest()


def shown_intent_fields(effects: Iterable[str]) -> tuple[str, ...]:
    """Terms these effects bind whenever the screen shows them."""
    shown = {f for e in effects for f in SHOWN_INTENT_FIELDS.get(e, ())}
    return tuple(f for f in ALL_INTENT_FIELDS if f in shown)


def intent_fields(effects: Iterable[str]) -> tuple[str, ...]:
    """The terms a message or payment among these effects must state."""
    needed = {f for e in effects for f in INTENT_FIELDS.get(e, ())}
    return tuple(f for f in ALL_INTENT_FIELDS if f in needed)


_CURRENCY_SIGNS = {
    "¥": "cny",
    "￥": "cny",
    "rmb": "cny",
    "元": "cny",
    "$": "usd",
    "€": "eur",
    "£": "gbp",
}


def _same_term(field: str, declared: str, shown: str) -> bool:
    """Whether what the screen shows is what the agent declared, allowing for the way
    screens format things: phone numbers, truncated previews, currency signs."""
    if field in ("address", "payment_method"):
        # Screens shorten both ("…3号楼", "美团支付 推荐"): one contains the other.
        a, b = (re.sub(r"[\W_]+", "", t.casefold()) for t in (declared, shown))
        return bool(a and b) and (a in b or b in a)
    if field in ("sender", "recipient", "payee"):
        a, b = re.sub(r"\D", "", declared), re.sub(r"\D", "", shown)
        if len(a) >= 7 and len(b) >= 7:
            short, long = sorted((a, b), key=len)
            return long.endswith(short)
        a, b = (re.sub(r"[\W_]+", "", t.casefold()) for t in (declared, shown))
        return bool(a and b) and (a in b or b in a)
    if field == "body":
        a, b = (" ".join(t.split()) for t in (declared, shown))
        preview = b.rstrip("…. ")
        return a == b or (len(preview) >= 20 and a.startswith(preview))
    if field == "amount":
        try:
            return Decimal(re.sub(r"[^\d.\-]", "", declared)) == Decimal(
                re.sub(r"[^\d.\-]", "", shown)
            )
        except InvalidOperation:
            return False
    a, b = (
        _CURRENCY_SIGNS.get(t.strip().casefold(), t.strip().casefold()) for t in (declared, shown)
    )
    return a == b


def check_intent(
    *,
    effects: Iterable[str],
    declared: Mapping[str, str | None] | None,
    seen: Mapping[str, str | None] | None,
) -> tuple[dict[str, str] | None, list[str]]:
    """The terms an approval of this input binds, and what the policy notes about them.

    A message or payment (by the agent's or the risk check's effects) must come with its
    terms, as the screen shows them; the independent risk check reads the same terms
    from the screenshot. Rejects a missing term (``intent_required``) and a term the
    screen contradicts (``intent_mismatch``). A term the screen does not show is noted,
    not rejected: the input is sensitive and waits for the user unless full access.

    The account a message is sent from, and a payment's delivery address and payment
    method, are bound when stated, and must be stated when the risk check sees them on
    the screen."""
    needed = intent_fields(effects)
    if not needed:
        return None, []
    stated = {f: str((declared or {}).get(f) or "").strip() for f in ALL_INTENT_FIELDS}
    missing = [f for f in needed if not stated[f]]
    if missing:
        raise Rejected(
            "intent_required",
            "state " + ", ".join(missing) + " in `intent`, exactly as the screen shows them",
        )
    optional = shown_intent_fields(effects)
    unstated = [f for f in optional if not stated[f] and str((seen or {}).get(f) or "").strip()]
    if unstated:
        raise Rejected(
            "intent_required",
            "the screen shows "
            + ", ".join(f"{f} {str((seen or {})[f]).strip()!r}" for f in unstated)
            + ": state it in `intent` as the screen shows it",
        )
    terms = {
        f: stated[f] for f in ALL_INTENT_FIELDS if f in needed or (f in optional and stated[f])
    }
    if seen is None:
        return terms, ["terms not checked against the screen"]
    unread = []
    for field in terms:
        shown = str(seen.get(field) or "").strip()
        if not shown:
            unread.append(field)
        elif not _same_term(field, terms[field], shown):
            raise Rejected(
                "intent_mismatch",
                f"{field}: you stated {terms[field]!r} but the screen shows {shown!r}",
            )
    if unread:
        return terms, ["not readable on the screen: " + ", ".join(unread)]
    return terms, ["terms match the screen"]


def validate_effects(effects: Iterable[str]) -> list[str]:
    """The model's declared effects, checked against the known set."""
    unknown = set(effects) - set(EFFECT_DEFINITIONS)
    if unknown:
        raise Rejected("invalid_effects", ",".join(sorted(unknown)))
    return sorted(set(effects))


# Input kinds the risk assessor looks at. Back, home and opening an app cannot by
# themselves pay, send or delete anything; a press, held or twice, can, like a tap, and a
# pinch touches the screen like a swipe.
ASSESSED_KINDS = frozenset(
    {
        "tap",
        "swipe",
        "long_press",
        "double_tap",
        "pinch",
        "type_text",
        "set_picker",
        "select_option",
    }
)
# Inputs aimed at one point of the screenshot.
POINT_KINDS = frozenset({"tap", "long_press", "double_tap", "pinch", "set_picker", "select_option"})

_TEXT_PATTERNS = {
    "phone_number": re.compile(r"(?<!\d)\+?\d[\d\s-]{7,}\d(?!\d)"),
    "email": re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}"),
    "money": re.compile(r"[$\u00a5\u20ac\u00a3]\s*\d|\d\s*(?:\u5143|\u5757|RMB|CNY|USD|EUR)\b"),
    "card_number": re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
}


def text_flags(payload: Mapping[str, Any]) -> list[str]:
    """Kinds of personal or financial data in text the agent wants to enter."""
    if payload.get("kind") == "type_text":
        text = str(payload.get("text", ""))
    elif payload.get("kind") in ("set_picker", "select_option"):
        text = str(payload.get("value", ""))
    else:
        return []
    return sorted(name for name, pattern in _TEXT_PATTERNS.items() if pattern.search(text))


# System permission prompts. Asking for tracking is never worth granting; these are
# granted, in the narrowest form, without asking in "auto" and "full".
DECLINED_PERMISSIONS = frozenset({"tracking"})
GRANTED_PERMISSIONS = frozenset(
    {
        "location",
        "camera",
        "microphone",
        "photos",
        "contacts",
        "calendar",
        "notifications",
        "bluetooth",
        "local_network",
    }
)


def permission_prompt(risk: RiskVerdict | None) -> PermissionRequest | None:
    """The permission prompt an input answers, as the independent check reported it; a
    failed check reports nothing."""
    return risk.permission if risk is not None and not risk.failed else None


def _answered_without_asking(prompt: PermissionRequest | None) -> bool:
    """Whether answering the prompt needs no approval in "auto" and "full": declining
    always, allowing only a kind in ``GRANTED_PERMISSIONS`` and not more than the
    narrowest option. Anything else (a broad grant, another kind) is judged as any input."""
    if prompt is None:
        return False
    return not prompt.grants or (prompt.category in GRANTED_PERMISSIONS and not prompt.broad)


def check_permission_prompt(*, mode: PermissionMode, risk: RiskVerdict | None) -> None:
    """In "auto" and "full" the agent may not allow what ``DECLINED_PERMISSIONS`` asks
    for: the input is refused and the agent declines instead. In "ask" the user decides
    every answer (``decide_policy``)."""
    prompt = permission_prompt(risk)
    if (
        mode != PermissionMode.ASK
        and prompt is not None
        and prompt.grants
        and prompt.category in DECLINED_PERMISSIONS
    ):
        raise Rejected(
            "permission_declined",
            f"this allows {prompt.category.replace('_', ' ')}, which the policy declines for "
            "you. Tap the option that does not allow it (Ask App Not to Track, Don't Allow)",
        )


def decide_policy(
    *,
    mode: PermissionMode,
    kind: str,
    declared_effects: list[str],
    risk: RiskVerdict | None,
    app_sensitive: bool,
    text_flags: list[str],
    found_rank: int | None = None,
) -> Policy:
    """Decides whether an input may run now or must wait for the user's approval.

        Three independent signals mark an input sensitive: the effects the model declared,
        the risk assessor's verdict (a failed assessment counts as sensitive), and
        deterministic rules (sensitive apps, personal data in typed text). Any one is enough.
        An input changes data if either the model or the risk assessor says change_data,
        or if it is sensitive (a failed assessment cannot rule a change out).

        - ``ask``: inputs that change data wait; looking around runs.
        - ``auto``: payments, deletions, sharing personal data and account changes wait,
          as do inputs in a sensitive app, typed money or card numbers, and anything the
          risk check could not assess or called sensitive without naming an effect.
          Messages and orders paid for in a later step go ahead.
        - ``full``: only payments wait; a failed assessment cannot rule a payment out.

        A tap, press, swipe, typed text or picker value without a verdict counts as a failed
        assessment: the policy never assumes an unassessed input is harmless.

        The agent finds an app by searching the installed apps (``find_app``) before it
        opens one: the search only reads, and runs in every mode. In ``ask``,
        opening a match below the best one (``found_rank`` > 0) waits, so the user confirms
        that choice; the best match opens, as an app the agent knew does. In ``auto`` and
        ``full`` the agent opens whichever match it chose: opening an app changes nothing
    , and every input in it is judged on its own.

        An input that answers a system permission prompt: ``ask`` holds every answer,
        declining included; ``auto`` and ``full`` let a decline run, and let an allow run when it
        is of a kind in ``GRANTED_PERMISSIONS`` and the narrowest option, which is not sharing
        data with someone else. Any other allow is judged as before.
    """
    if risk is None and kind in ASSESSED_KINDS:
        risk = RiskVerdict(sensitive=True, effects=[], reason="not assessed", failed=True)
    prompt = permission_prompt(risk)
    easy = _answered_without_asking(prompt)
    effects = set(declared_effects) | set(risk.effects if risk else [])
    risk_sensitive = risk is not None and risk.sensitive
    if easy and mode != PermissionMode.ASK:
        effects.discard("share_data")  # an app asking for access is not sharing with someone
        # What the check calls sensitive here is the prompt itself, unless it names more.
        named = set(risk.effects) if risk else set()
        risk_sensitive = risk_sensitive and bool(named - {"share_data"})
    reasons: list[str] = []
    if declared_effects:
        reasons.append("agent declared: " + ", ".join(sorted(declared_effects)))
    if risk is not None and risk.failed:
        reasons.append("risk check unavailable: " + (risk.reason or "no verdict"))
    elif risk is not None and (risk.sensitive or risk.effects):
        reasons.append("risk check: " + (risk.reason or ", ".join(sorted(risk.effects))))
    if app_sensitive:
        reasons.append("the app is marked sensitive")
    if text_flags:
        reasons.append("text contains: " + ", ".join(text_flags).replace("_", " "))

    sensitive = bool(
        effects & SENSITIVE_EFFECTS
        or app_sensitive
        or text_flags
        or risk_sensitive
        or (risk is not None and risk.failed)
    )
    payment = is_payment(declared_effects, risk)
    changes = sensitive or CHANGE_EFFECT in effects

    if mode == PermissionMode.ASK:
        decision = "ask" if changes or prompt is not None else "allow"
        if changes:
            reasons.insert(0, "mode: ask before any change")
        elif prompt is not None:
            reasons.insert(0, "mode: ask before answering a permission prompt")
    elif mode == PermissionMode.AUTO:
        unexplained = risk_sensitive and not set(risk.effects if risk else []) & SENSITIVE_EFFECTS
        held = bool(
            effects & AUTO_ASK_EFFECTS
            or app_sensitive
            or set(text_flags) & MONEY_FLAGS
            or (risk is not None and risk.failed)
            or unexplained
        )
        decision = "ask" if held else "allow"
    else:
        decision = "ask" if payment else "allow"
        if payment:
            reasons.insert(0, "payments always need approval")
    if found_rank is not None:
        reasons.append(f"opens match {found_rank + 1} of an app search")
        if mode == PermissionMode.ASK and found_rank > 0:
            if decision != "ask":
                reasons.insert(0, "confirm the app the search found: not its best match")
            decision = "ask"
    return Policy(
        mode=mode,
        decision=decision,
        reasons=reasons,
        declared_effects=sorted(declared_effects),
        risk=risk,
        app_sensitive=app_sensitive,
        text_flags=list(text_flags),
        found_rank=found_rank,
    )


def reapply_policy(policy: Policy, mode: PermissionMode, kind: str) -> Policy:
    """The same signals judged under another mode (used when the user switches modes)."""
    return decide_policy(
        mode=mode,
        kind=kind,
        declared_effects=policy.declared_effects,
        risk=policy.risk,
        app_sensitive=policy.app_sensitive,
        text_flags=policy.text_flags,
        found_rank=policy.found_rank,
    )


def found_rank(app_id: str, searches: Sequence[Sequence[Mapping[str, Any]]]) -> int | None:
    """Where ``app_id`` stands in the newest search that found it (0 = best match), or
    None if no search did. ``searches`` lists find_app results, newest first."""
    for found in searches:
        ids = [str(app["app_id"]) for app in found]
        if app_id in ids:
            return ids.index(app_id)
    return None


def needs_approval(action: Action) -> bool:
    if action.policy is None:
        return bool(action.effects)
    return action.policy.decision == "ask"


def _points(payload: Mapping[str, Any]) -> list[tuple[float, float]]:
    kind = payload["kind"]
    if kind in POINT_KINDS:
        return [(payload["x"], payload["y"])]
    if kind == "type_text" and payload.get("field_x") is not None:
        return [(payload["field_x"], payload["field_y"])]
    if kind == "swipe":
        # A path (a person's drag, #211) must stay on the screen too: every point counts.
        along = [(p["x"], p["y"]) for p in payload.get("path") or []]
        return [(payload["x1"], payload["y1"]), *along, (payload["x2"], payload["y2"])]
    return []


def check_points(payload: Mapping[str, Any], width: int, height: int) -> None:
    """Every point an input aims at must be on the picture its coordinates refer to (the
    agent's screenshot, a takeover's frame), ``width`` by ``height`` pixels;
    Rejected("out_of_bounds") otherwise."""
    for x, y in _points(payload):
        if not (0 <= x < width and 0 <= y < height):
            raise Rejected("out_of_bounds", f"({x}, {y}) is outside {width}x{height}")


def admit_input(
    *,
    payload: Mapping[str, Any],
    device: Device,
    observation: Observation | None,
    latest_observation_id: UUID | None,
    lease_epoch: int,
    last_input_at: datetime | None,
    device_busy: bool,
    unknown_pending: bool,
    now: datetime,
    max_age: timedelta,
) -> None:
    """Decides whether a proposed device input may be queued.

    Every input must refer to the newest complete screenshot of the device, taken
    under the current lease epoch and after the previous input. Coordinates must lie
    inside that screenshot. Any unresolved UNKNOWN effect on the device blocks input.
    """
    kind = payload["kind"]
    if kind in PERSON_ONLY_KINDS:
        raise Rejected("unsupported_input", f"only a person taking the phone over presses {kind}")
    if kind not in device.capabilities:
        raise Rejected("unsupported_input", f"{device.platform} cannot {kind}")
    if not device.online:
        raise Rejected("device_offline")
    if not device.enabled:
        raise Rejected("device_not_enabled", "an owner enables devices on the Web app")
    if device.problems:
        raise Rejected("device_not_ready", ", ".join(device.problems))
    if unknown_pending:
        raise Rejected("unknown_effect_pending", "a previous action has an unknown effect")
    if device_busy:
        raise Rejected("device_busy", "wait for the previous action's result")
    if observation is None or observation.device_id != device.id:
        raise Rejected("observation_required", "observe the device first")
    if observation.status != ObservationStatus.COMPLETE or observation.screen is None:
        raise Rejected("observation_incomplete")
    if observation.id != latest_observation_id:
        raise Rejected("stale_observation", "a newer screenshot exists")
    if observation.epoch != lease_epoch:
        raise Rejected("stale_observation", "taken under a previous device lease")
    assert observation.captured_at is not None
    if now - observation.captured_at > max_age:
        raise Rejected("stale_observation", "screenshot is too old")
    if last_input_at is not None and observation.captured_at <= last_input_at:
        raise Rejected("stale_observation", "the screen changed since this screenshot")
    check_points(payload, observation.screen.width, observation.screen.height)


# ---------------------------------------------------------------- going in circles

# A cycle of inputs repeated this often since the user's last message: the agent is warned
# with the input's result, then stopped to ask the user.
LOOP_WARN = 4
LOOP_ASK = 8
# Taps on the same row count as the same input (an agent aims at the same element from
# slightly different x positions); screenshot pixels.
LOOP_Y_TOLERANCE = 40
# Swipes, back and home move around a screen and break a cycle: scrolling is not a loop.
_LOOPING_KINDS = POINT_KINDS | {"type_text", "launch_app"}


def _same_input(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    kind = a.get("kind")
    if kind != b.get("kind") or kind not in _LOOPING_KINDS:
        return False
    if kind in POINT_KINDS:
        return abs(float(a["y"]) - float(b["y"])) <= LOOP_Y_TOLERANCE and a.get("value") == b.get(
            "value"
        )
    if kind == "type_text":  # typed another way is trying something else
        return a.get("text") == b.get("text") and (a.get("method") or "set") == (
            b.get("method") or "set"
        )
    return a.get("app_id") == b.get("app_id")


def check_changed(
    payload: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    before: str | None,
    after: str | None,
) -> None:
    """Refuses an input aimed at the same point as the previous one when the screenshot
    that input left is the very picture it was made on (``before`` and ``after`` are
    their SHA-256), and this input is made on that screenshot. Some changes show a second
    or two late (a switch, a theme): a second tap would undo the first, so the agent
    observes first and decides on what it sees then."""
    if previous is None or before is None or before != after:
        return
    kind = payload.get("kind")
    if kind not in POINT_KINDS or previous.get("kind") != kind:
        return
    points = [(payload.get(k), previous.get(k)) for k in ("x", "y")]
    if all(
        a is not None and b is not None and abs(float(a) - float(b)) <= LOOP_Y_TOLERANCE
        for a, b in points
    ):
        raise Rejected(
            "unchanged_screen",
            "the screen has not changed at all since your last input here; some changes "
            "show a second or two later, and doing it again may undo it: call observe first",
        )


# Turns in a row that only searched the web before the agent must ask the user.
SEARCH_TURNS = 3


def searching_in_circles(only_searched: Sequence[bool]) -> bool:
    """Whether the agent searched the web in circles: each of the last SEARCH_TURNS
    turns, this one last, only searched (called none of our tools). Then it stops and
    asks instead of searching on at the user's expense."""
    recent = only_searched[-SEARCH_TURNS:]
    return len(recent) == SEARCH_TURNS and all(recent)


def repeats(inputs: Sequence[Mapping[str, Any]]) -> int:
    """How many times the newest cycle of two to four inputs repeats back to back at the
    end of ``inputs`` (one device's inputs since the user's last message, oldest first).
    One input repeated on its own (tapping "+" five times) is not a cycle."""
    best = 1
    for size in (2, 3, 4):
        last = inputs[-size:]
        if len(last) < size or all(_same_input(last[0], other) for other in last[1:]):
            continue
        count = 1
        while len(inputs) >= size * (count + 1) and all(
            _same_input(inputs[-1 - i], inputs[-1 - i - size * count]) for i in range(size)
        ):
            count += 1
        best = max(best, count)
    return best


def result_effect(outcome: str, kind: str) -> Effect:
    """Maps a Gateway dispatch outcome to the business-effect state. A search of the
    installed apps reads, never writes: it has no effect."""
    if kind == "find_app" and outcome == "returned":
        return Effect.NO_EFFECT
    return {
        "rejected": Effect.NOT_ATTEMPTED,
        "returned": Effect.UNVERIFIED,
        "unknown": Effect.UNKNOWN,
    }[outcome]


def approval_status(approval: Approval, now: datetime) -> str:
    if approval.used_at is not None:
        return "used"
    if approval.decision == "deny":
        return "denied"
    if approval.decision == "approve":
        return "approved" if now < approval.expires_at else "expired"
    return "pending" if now < approval.expires_at else "expired"


def check_deliverable(
    action: Action,
    *,
    approval: Approval | None,
    lease_epoch: int | None,
    control_revision: int,
    now: datetime,
) -> None:
    """Re-checked atomically right before the hub hands an action to the Gateway."""
    if action.status != ActionStatus.QUEUED:
        raise Rejected("not_queued")
    if lease_epoch != action.epoch:
        raise Rejected("lease_changed")
    if control_revision != action.control_revision:
        raise Rejected("task_changed")
    if needs_approval(action):
        if approval is None or approval_status(approval, now) != "approved":
            raise Rejected("approval_invalid")
        # The approval covers the parameters as they are now, not only as they were
        # stored: the digest is computed again from them.
        current = action_digest(
            action.id, action.device_id, action.epoch, action.input, action.intent
        )
        if not approval.digest == action.digest == current:
            raise Rejected("approval_mismatch")


def retry_of(previous: Sequence[Action]) -> UUID | None:
    """The action a new input on a device tries again: the device's latest
    action in the task, if a person confirmed from a later screenshot that it had no
    effect. Only then may the same effect be attempted anew; an unknown one blocks."""
    if previous and previous[-1].effect == Effect.NO_EFFECT and previous[-1].reconciled_by:
        return previous[-1].id
    return None


def check_reconcile(action: Action, observation: Observation) -> None:
    """A user may settle an UNKNOWN effect only by pointing at a newer screenshot."""
    if action.effect != Effect.UNKNOWN:
        raise Rejected("not_unknown")
    if observation.device_id != action.device_id:
        raise Rejected("wrong_device")
    if observation.status != ObservationStatus.COMPLETE or observation.captured_at is None:
        raise Rejected("observation_incomplete")
    if action.finished_at is None or observation.captured_at <= action.finished_at:
        raise Rejected("observation_too_old", "needs a screenshot taken after the action")


def _number_like(text: str) -> str:
    """The digits of a value that is mostly digits (a phone number, a code), else ""."""
    digits = re.sub(r"\D", "", text)
    letters = re.sub(r"[\W_]", "", text)
    return digits if len(digits) >= 7 and len(digits) >= 0.7 * len(letters) else ""


def typed_text_shown(typed: str, field: str) -> bool:
    """Whether the field read back after typing still holds the typed text. Fields
    reformat what they get, so a number-like text is compared by its digits (spaces,
    dashes and a country code added by the field are fine) and other text ignoring
    spaces, punctuation and case. The field may hold more than was typed."""
    digits = _number_like(typed)
    if digits:
        return digits in re.sub(r"\D", "", field)
    plain = [re.sub(r"[\W_]+", "", t.casefold()) for t in (typed, field)]
    return plain[0] in plain[1]


# Why typing on the keyboard or pasting stopped short, told to the agent.
_TYPING_STOPPED = {
    "no_candidate": "The keyboard offered no candidate showing the next characters, so "
    "typing stopped there; field_shows has what went in.",
    "not_on_keyboard": "A character of the text is not on the keyboard, so typing stopped "
    "there. Type the rest with method 'paste', or without that character.",
    "no_pinyin_keyboard": "Nothing was typed: the phone has no Chinese pinyin keyboard. "
    "Use method 'paste', or ask the user to add the keyboard.",
    "no_keyboard": "Nothing was typed: no on-screen keyboard is up. Tap the field first.",
    "too_long_for_keys": "Nothing was typed: that is too long to tap in. Use 'paste'.",
    "field_not_empty": "Nothing was pasted: the field holds text. Paste with replace true, "
    "or type with another method.",
    "no_paste_menu": "Nothing was pasted: the field showed no Paste in its menu.",
    "pasteboard_not_set": "Nothing was pasted: the text could not be put on the phone's "
    "pasteboard. Type it with method 'keys'.",
    "paste_not_taken": "Paste was tapped, but the field did not take the text. If the new "
    "screenshot does not show it, type it with method 'keys'.",
    "field_not_cleared": "Nothing was typed: what the field held could not all be deleted; "
    "field_shows has what is left. Empty the field yourself, then type with replace false.",
}


def typing_note(method: str, problem: str | None, read_back: bool) -> str | None:
    """What the agent is told after typing: why it stopped short, or that the field
    could not be read back after text was set, which is how a box that closes when text
    is set into it shows."""
    if problem in _TYPING_STOPPED:
        return _TYPING_STOPPED[problem]
    if method == "set" and not read_back and problem is None:
        return (
            "The field could not be read back after typing: it may have closed and lost the "
            "text. If the new screenshot does not show your text, open the field again and "
            "type it with method 'keys' (or 'paste')."
        )
    return None


# A completion refused because the goal is not met this many times in a run; the
# next refusal asks the user how to go on.
COMPLETION_REFUSALS = 2
# Turns in a row that end without a tool call while the goal is not met: the agent is not
# getting anywhere, so it stops for the user instead of being sent back again.
GOAL_STALL = 3


# ---------------------------------------------------------------- long-term memory (§8.2)

MEMORY_MAX = 300
MEMORY_KEPT_MAX = 50  # preferences and corrections; each task's first request carries them
MEMORY_APP_MAX = 30  # notes about one app; given when the agent first sees it
MEMORY_APPS_MAX = 300  # notes about all apps together
MEMORY_KINDS = ("user", "feedback", "app")
_APP_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,199}$")
# What memory never holds: secrets and one-time permissions.
_SECRET_WORDS = re.compile(
    r"密码|口令|验证码|校验码|支付密码|(pin|安全|解锁|动态|短信|登录|提取|取件|门禁)码|密钥|"
    r"cvv|cvc|(?<![a-z])pin(?![a-z])|password|passcode|one-time code|otp|"
    r"verification code|security code|door code|unlock code|secret|token|api[ _-]?key",
    re.IGNORECASE,
)


def check_memory(text: str, kept: int = 0, *, app: str | None = None) -> str:
    """An entry for long-term memory: short, and never a password, code or card number.
    ``kept``: the entries of its kind there are already (of that app, for an app note)."""
    cleaned = " ".join(text.split())
    if not 1 <= len(cleaned) <= MEMORY_MAX:
        raise Rejected("invalid_memory", f"1 to {MEMORY_MAX} characters")
    if _SECRET_WORDS.search(cleaned) or "card_number" in text_flags(
        {"kind": "type_text", "text": cleaned}
    ):
        raise Rejected(
            "memory_not_allowed", "passwords, codes and payment details are never remembered"
        )
    limit = MEMORY_APP_MAX if app else MEMORY_KEPT_MAX
    if kept >= limit:
        raise Rejected(
            "memory_full", f"at most {limit} entries; bring one up to date with replaces"
        )
    return cleaned


def check_memory_kind(kind: str, app: str | None) -> str | None:
    """An entry's kind, and the app an "app" note is about: its package or bundle ID, as
    the screenshot names the foreground app. Other kinds name no app."""
    if kind not in MEMORY_KINDS:
        raise Rejected("invalid_memory", "kind is user, feedback or app")
    if kind != "app":
        return None
    name = app.strip() if isinstance(app, str) else ""
    if not _APP_ID.match(name):
        raise Rejected(
            "invalid_memory", "an app note names the app's package, as on the screenshot"
        )
    return name


# ---------------------------------------------------------------- the person's own skills

# A skill may say how to handle a password or a code ("hand the payment password to the
# user"); it never holds one: a secret word followed by a value with a digit in it.
_SECRET_VALUE = re.compile(
    rf"(?:{_SECRET_WORDS.pattern})\s*(?:是|为|[:\uff1a=]|is)\s*(?=\S*\d)[A-Za-z0-9!@#$%^&*._\-]{{4,}}",
    re.IGNORECASE,
)


def check_skill(description: str, apps: Iterable[str], body: str) -> tuple[str, tuple[str, ...]]:
    """A skill the person saves in this WitX: its one-line description and the apps
    it is about (packages or bundle IDs). It holds no password, code or card number."""
    cleaned = " ".join(description.split())
    named = tuple(dict.fromkeys(a.strip() for a in apps if a.strip()))
    for app in named:
        if not _APP_ID.match(app):
            raise Rejected("invalid_skill", f"{app!r} is not an app's package or bundle ID")
    text = f"{cleaned}\n{body}"
    if _SECRET_VALUE.search(text) or _TEXT_PATTERNS["card_number"].search(text):
        raise Rejected(
            "skill_not_allowed", "a skill never holds passwords, codes or payment details"
        )
    return cleaned, named


# ---------------------------------------------------------------- plans

PLAN_STATUSES = ("pending", "in_progress", "done")
PLAN_MAX_STEPS = 20


def check_plan(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The agent's plan: 1 to PLAN_MAX_STEPS short steps, each pending, in progress or
    done, at most one in progress."""
    if not 1 <= len(steps) <= PLAN_MAX_STEPS:
        raise Rejected("invalid_plan", f"a plan has 1 to {PLAN_MAX_STEPS} steps")
    plan = []
    for entry in steps:
        text = " ".join(str(entry.get("step", "")).split())[:200]
        status = str(entry.get("status", ""))
        if not text or status not in PLAN_STATUSES:
            raise Rejected("invalid_plan", "each step needs text and a status")
        plan.append({"step": text, "status": status})
    if sum(p["status"] == "in_progress" for p in plan) > 1:
        raise Rejected("invalid_plan", "at most one step is in progress")
    return plan


# ---------------------------------------------------------------- context compression

# A new summary must take in at least this many more steps than the last attempt.
COMPRESS_MIN_STEPS = 4


def compression_point(
    seqs: Sequence[int],
    *,
    last_attempt: int | None,
    last_input_tokens: int | None,
    after_steps: int,
    at_tokens: int,
    keep_steps: int,
) -> int | None:
    """The step through whose input a run's history should be summarized now, or None.

    ``seqs`` are the run's completed steps, oldest first; the first one (the task as the
    run began) is never summarized and the newest ``keep_steps`` stay word for word. Due
    once ``after_steps`` steps passed since the last attempt (one that failed too, so a
    failing call is not retried every step), or once a step's input reached
    ``at_tokens``; and only if it takes in at least COMPRESS_MIN_STEPS more steps."""
    since = [s for s in seqs[1:] if last_attempt is None or s > last_attempt]
    due = len(since) >= after_steps or (last_input_tokens or 0) >= at_tokens
    if not due or len(seqs) < keep_steps + 2:
        return None
    point = seqs[-(keep_steps + 1)]
    taken = [s for s in seqs[1:] if s <= point and (last_attempt is None or s > last_attempt)]
    return point if len(taken) >= COMPRESS_MIN_STEPS else None


def last_inputs(actions: Iterable[Action]) -> dict[UUID, datetime]:
    """When each device last received an input that reached it."""
    last: dict[UUID, datetime] = {}
    for a in actions:
        if a.finished_at is not None and a.effect != Effect.NOT_ATTEMPTED:
            last[a.device_id] = max(a.finished_at, last.get(a.device_id, a.finished_at))
    return last


def check_select(kind: str, declared: Iterable[str]) -> None:
    """An option chosen by its text is tapped where the Gateway finds it, not where the
    agent pointed, so it cannot be one that pays, orders, sends or deletes: those are
    tapped with tap, on the screenshot, where the approval is bound to what is shown."""
    if kind == "select_option" and list(declared):
        raise Rejected(
            "select_not_allowed",
            "an option with an effect (pay, order, send, delete...) is tapped with tap",
        )


def completion_gaps(
    *,
    actions: Iterable[Action],
    evidence: Iterable[Observation],
    device_ids: Iterable[UUID],
) -> list[str]:
    """Reasons a run may not complete yet. An empty list means it may."""
    gaps: list[str] = []
    actions = list(actions)
    evidence = list(evidence)
    if any(a.status in OPEN_ACTION for a in actions):
        gaps.append("an action is still pending")
    if any(a.effect == Effect.UNKNOWN for a in actions):
        gaps.append("an action has an unknown effect")
    last_input = last_inputs(actions)
    if list(device_ids) and not evidence:
        gaps.append("cite at least one screenshot that shows the goal is met")
    # What the phone shows now needs one screenshot taken after the last input on it. An
    # earlier screenshot may be cited as well, for a change made before.
    current: set[UUID] = set()
    for o in evidence:
        if o.status != ObservationStatus.COMPLETE or o.captured_at is None:
            gaps.append(f"observation {o.id} is not complete")
        elif o.device_id not in last_input or o.captured_at > last_input[o.device_id]:
            current.add(o.device_id)
    if evidence:
        for device_id in last_input.keys() - current:
            gaps.append(
                f"cite a screenshot of device {device_id} taken after your last input on it"
            )
    return gaps


# ---------------------------------------------------------------- idempotency keys

# How long a write request's Idempotency-Key is remembered.
IDEMPOTENCY_TTL = timedelta(days=1)

# The most days a person may keep their finished tasks' screenshots.
SCREENSHOT_RETENTION_MOST = 30


def retention_most(deployment_days: int) -> int:
    """The most a person may choose: 30 days, or fewer where the deployment keeps
    screenshots for less."""
    return max(1, min(SCREENSHOT_RETENTION_MOST, deployment_days))


def request_digest(content: Mapping[str, Any]) -> str:
    """SHA-256 of a request's content, independent of key order and spacing."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_key_reuse(*, stored_request: str, stored_digest: str, request: str, digest: str) -> None:
    """An Idempotency-Key may only be sent again with the same request and content."""
    if (stored_request, stored_digest) != (request, digest):
        raise Rejected(
            "idempotency_key_reused", "this Idempotency-Key was sent with another request"
        )


# ---------------------------------------------------------------- takeover

# A takeover ends after this long without input from the person.
TAKEOVER_IDLE = timedelta(minutes=2)
# The phone's side keys: only a person taking it over presses them. The agent has no tool
# for them and is not told the phone has them; an input of the agent's that names one is
# refused. Locking the phone, above all, would end its own work.
PERSON_ONLY_KINDS = frozenset({"volume_up", "volume_down", "lock"})
# What a person may do during a takeover: a click, a held click and a drag on the picture,
# typed text, the system buttons and the side keys.
MANUAL_INPUT_KINDS = (
    frozenset({"tap", "long_press", "swipe", "type_text", "back", "home"}) | PERSON_ONLY_KINDS
)


def takeover_active(lease: Lease, now: datetime) -> bool:
    """Whether a person controls the lease's device right now."""
    return (
        lease.takeover_id is not None
        and lease.takeover_expires_at is not None
        and lease.takeover_expires_at > now
    )


GOAL_COMMAND = "/goal"


@dataclass(frozen=True)
class GoalCommand:
    """What a ``/goal`` message asks: set (or rewrite) the goal, clear it, pause it or
    resume it."""

    kind: Literal["set", "clear", "pause", "resume"]
    goal: str = ""


def goal_command(text: str) -> GoalCommand | None:
    """The ``/goal …`` command in a message from the user, None for any other message:
    ``/goal clear`` (no goal: the agent follows the conversation), ``/goal pause`` and
    ``/goal resume``, or ``/goal <text>`` for a new goal. Only the user changes a goal."""
    head, *rest = text.strip().split(maxsplit=1)
    if head.lower() != GOAL_COMMAND:
        return None
    goal = rest[0].strip() if rest else ""
    if not goal:
        raise Rejected("invalid_arguments", "write the new goal after /goal")
    words: dict[str, Literal["clear", "pause", "resume"]] = {
        "clear": "clear",
        "pause": "pause",
        "resume": "resume",
    }
    word = words.get(goal.lower())
    return GoalCommand(word) if word else GoalCommand("set", goal[:4000])


PLAN_COMMAND = "/plan"


@dataclass(frozen=True)
class PlanCommand:
    """What a ``/plan`` message asks: plan first (``text`` is what to plan), go ahead with
    the plan the agent presented, or leave plan mode without a plan."""

    kind: Literal["start", "approve", "off"]
    text: str = ""


def plan_command(text: str) -> PlanCommand | None:
    """The ``/plan …`` command in a message from the user, None for any other message.
    Only the user's messages are read as commands: nothing the agent or a screen says can
    approve a plan."""
    head, *rest = text.strip().split(maxsplit=1)
    if head.lower() != PLAN_COMMAND:
        return None
    body = rest[0].strip() if rest else ""
    if not body:
        raise Rejected("invalid_arguments", "write what to plan after /plan")
    word = body.lower()
    if word == "approve":
        return PlanCommand("approve")
    if word == "off":
        return PlanCommand("off")
    return PlanCommand("start", body[:4000])


SKILL_COMMAND = "/skill"


def skill_command(text: str) -> str | None:
    """The ``/skill …`` command in a message from the user, None for any other
    message: the agent drafts a skill, which only the person saves. In a task, ``/skill``
    alone keeps how this task was done, and what follows it says what to keep in mind;
    as a task's first message, what follows says what the skill is for."""
    head, *rest = text.strip().split(maxsplit=1)
    if head.lower() != SKILL_COMMAND:
        return None
    return rest[0].strip()[:4000] if rest else ""


# ``$name`` or ``$plugin:name`` in a user's message, starting with a letter: not
# part of a Latin word (a$b) and not an amount ($5); right after Chinese text it counts
# ("用$open-wifi").
_NAMED_SKILL = re.compile(
    r"(?<![A-Za-z0-9_$])\$([a-z][a-z0-9]*(?:-[a-z0-9]+)*(?::[a-z0-9]+(?:-[a-z0-9]+)*)?)"
    r"(?![A-Za-z0-9_-])"
)


def named_skills(text: str) -> list[str]:
    """The skills a message from the user names, as written, each once."""
    return list(dict.fromkeys(_NAMED_SKILL.findall(text)))


def named_skill(name: str, skills: Collection[str]) -> str | None:
    """The full name (``plugin:skill``) of a skill named ``name``: itself, or the one skill
    with that name in any plugin; None when there is no such skill, or several."""
    if name in skills:
        return name
    found = [s for s in skills if s.split(":", 1)[-1] == name]
    return found[0] if len(found) == 1 else None


def check_only_looking(
    *,
    kind: str,
    declared_effects: list[str],
    risk: RiskVerdict | None,
    app_sensitive: bool,
    text_flags: list[str],
) -> None:
    """In plan mode the agent may look around (open apps, scroll, search) but not change
    anything: an input that the ``ask`` policy would hold for approval, because it could
    change something or could not be assessed, is refused instead. The same signals
    as for any input decide (declared effects, the independent risk check, sensitive
    apps, personal data in typed text)."""
    policy = decide_policy(
        mode=PermissionMode.ASK,
        kind=kind,
        declared_effects=declared_effects,
        risk=risk,
        app_sensitive=app_sensitive,
        text_flags=text_flags,
    )
    if policy.decision == "ask":
        raise Rejected(
            "plan_mode",
            "plan mode: this input could change something ("
            + "; ".join(policy.reasons)
            + "). Until the user approves your plan you may only look around: open apps, "
            "scroll, search. Call present_plan when you have seen enough",
        )


def active_ms(
    work: Sequence[tuple[datetime, datetime]],
    windows: Sequence[tuple[datetime, datetime]],
) -> int:
    """How long the agent worked within the windows, in milliseconds: the union of its
    work (model calls, inputs on phones, screenshots; they may overlap) clipped to the
    windows (e.g. while a goal was set and not paused). Waiting is not work."""
    merged: list[list[datetime]] = []
    for start, end in sorted(w for w in work if w[1] > w[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total = 0.0
    for start, end in merged:
        for w_start, w_end in windows:
            overlap = (min(end, w_end) - max(start, w_start)).total_seconds()
            if overlap > 0:
                total += overlap
    return round(total * 1000)


def goal_windows(
    created_at: datetime,
    first_goal: bool,
    changes: Sequence[tuple[datetime, str]],
    now: datetime,
) -> list[tuple[datetime, datetime]]:
    """When the task's goal was in force: from when it was set (the start, if the first
    message set it) to when it was cleared, without the time it was paused. ``changes``
    are ("set" | "clear" | "pause" | "resume") in time order."""
    windows: list[tuple[datetime, datetime]] = []
    since = created_at if first_goal else None
    for at, change in changes:
        if change in ("set", "resume"):
            since = since or at
        elif since is not None:  # clear or pause
            windows.append((since, at))
            since = None
    if since is not None:
        windows.append((since, now))
    return windows


def check_agent_goal(current: str, ever_set: bool) -> None:
    """The agent may give a task a goal only while it has none and never had one: a goal
    the user set is theirs to change, and one they cleared stays cleared."""
    if current:
        raise Rejected("goal_exists", "the task has a goal; only the user changes it")
    if ever_set:
        raise Rejected("goal_cleared", "the user cleared the goal; follow their messages")


def held_by_nobody(lease: Lease | None, now: datetime) -> bool:
    """No task holds the device and no one controls it. A person's lease whose takeover
    ran out (its connection was lost with the process that served it) holds nothing."""
    return lease is None or (lease.task_id is None and not takeover_active(lease, now))


def admit_device_takeover(device: Device, lease: Lease | None, now: datetime) -> None:
    """A person may take over a device outside any task when it is enabled, usable and
    free. A device a task holds is taken over from that task, which pauses first."""
    if not device.enabled:
        raise Rejected("device_not_enabled", f"{device.name}: an owner enables it first")
    require_usable(device, takeover=True)
    if lease is not None and lease.task_id is not None:
        raise Rejected("device_busy", "a task holds this phone: take it over from the task")
    if not held_by_nobody(lease, now):
        raise Rejected("takeover_active", "someone else controls this phone")


# ---------------------------------------------------------------- browser sessions

# A session ends after this long without a request, and in any case after SESSION_MAX.
SESSION_IDLE = timedelta(hours=12)
SESSION_MAX = timedelta(days=7)
# Requests that change nothing; every other cookie-authenticated request needs the CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def session_valid(
    *, seen_at: datetime, expires_at: datetime, revoked_at: datetime | None, now: datetime
) -> bool:
    return revoked_at is None and now < expires_at and now - seen_at < SESSION_IDLE


def safe_return_path(path: str | None) -> str:
    """Where to go after signing in: only a path on this site (never another host)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


# ---------------------------------------------------------------- Gateway identity

# How long a one-time enrollment code can be used.
ENROLL_CODE_TTL = timedelta(hours=24)
# A Gateway asks for its next credential once its current one is this old.
CREDENTIAL_ROTATE_AFTER = timedelta(days=30)


def credential_due(issued_at: datetime | None, now: datetime) -> bool:
    return issued_at is None or now - issued_at >= CREDENTIAL_ROTATE_AFTER


def parse_enrollment_code(code: str) -> tuple[UUID, str]:
    """An enrollment code is "<tenant id>.<secret>": the tenant lets the cloud look the
    code up under that tenant's row-level security, with no cross-tenant query."""
    tenant, _, secret = code.strip().partition(".")
    try:
        tenant_id = UUID(tenant)
    except ValueError:
        raise Rejected("invalid_code") from None
    if len(secret) < 32:
        raise Rejected("invalid_code")
    return tenant_id, secret


# ---------------------------------------------------------------- a task's page in parts

# A part of a long task's conversation holds about this many actions or rounds.
PART_ACTIONS = 200
PART_ROUNDS = 20


def part_start(
    round_starts: Sequence[datetime],
    action_times: Sequence[datetime],
    before: datetime | None = None,
) -> datetime | None:
    """Where the part of a task's conversation that ends at ``before`` starts (the
    latest part, without it). A part starts at a round, the start of a run of the main
    agent: it takes rounds from the newest back while it holds no more than
    ``PART_ACTIONS`` actions and ``PART_ROUNDS`` rounds, and always one. None: it
    reaches back to the task's start, so nothing is earlier."""
    rounds = sorted((t for t in round_starts if before is None or t < before), reverse=True)
    times = sorted(t for t in action_times if before is None or t < before)
    start = None
    for taken, at in enumerate(rounds, start=1):
        if taken > 1 and len(times) - bisect_left(times, at) > PART_ACTIONS:
            break  # this round would make the part too long
        start = at
        if taken == PART_ROUNDS:
            break
    return None if start is None or start == rounds[-1] else start


# ---------------------------------------------------------------- deletion and retention (§19)


def check_task_deletable(
    *, status: TaskStatus, unknown_pending: bool, is_creator: bool, is_owner: bool
) -> None:
    """Only a finished task can be deleted, and never while an action on it has an
    unknown effect: deleting it would unlock the device without a verdict."""
    if not (is_creator or is_owner):
        raise Rejected("owner_required", "only its creator or a tenant owner can delete a task")
    if status not in TERMINAL_TASK:
        raise Rejected("task_not_finished", "stop the task before deleting it")
    if unknown_pending:
        raise Rejected(
            "unknown_effect_pending", "reconcile the unknown action with a new screenshot first"
        )


# ---------------------------------------------------------------- models


def check_model(model: str, catalog: Collection[str]) -> None:
    if model not in catalog:
        raise Rejected("unknown_model", f"{model} is not in the model catalog")


def allowed_providers(chosen: Sequence[str] | None, default_provider: str) -> frozenset[str]:
    """The providers a tenant's screenshots and text may go to: the
    owner's choice, or, until an owner chooses, only the default model's provider."""
    return frozenset(chosen) if chosen is not None else frozenset({default_provider})


def check_provider(provider: str, allowed: Collection[str]) -> None:
    """A model is used only if the tenant allows its provider: no task, switch, fallback,
    risk check or side chat sends anything to another one."""
    if provider not in allowed:
        raise Rejected(
            "provider_not_allowed", f"an owner has not allowed {provider} for this organisation"
        )


def fallback_model(
    *,
    current: str,
    failed: Collection[str],
    catalog: Sequence[tuple[str, str]],
    allowed: Collection[str],
) -> str | None:
    """The model a task falls back to when its model keeps failing: the first
    in the catalog's order, other than the current one and the ones that already failed
    since the run's last successful step, whose provider the tenant allows. None when
    there is none: the run then waits for a person. ``catalog`` is (name, provider)."""
    for name, provider in catalog:
        if name != current and name not in failed and provider in allowed:
            return name
    return None


def check_provider_choice(chosen: Sequence[str], known: Collection[str]) -> list[str]:
    """An owner's choice of providers: at least one, each used by the model catalog."""
    picked = sorted(set(chosen))
    if not picked:
        raise Rejected("invalid_providers", "allow at least one provider")
    unknown = set(picked) - set(known)
    if unknown:
        raise Rejected(
            "invalid_providers", "not in the model catalog: " + ", ".join(sorted(unknown))
        )
    return picked


# ---------------------------------------------------------------- approval channels


def is_payment(declared_effects: Iterable[str], risk: RiskVerdict | None) -> bool:
    """A purchase or payment by any signal; a failed risk check cannot rule one out."""
    effects = set(declared_effects) | set(risk.effects if risk else [])
    return bool(effects & PAYMENT_EFFECTS) or (risk is not None and risk.failed)


APPROVAL_CHANNELS = ("web", "mcp")


def check_approval_channel(*, payment: bool, via: str) -> None:
    """Payments are approved on the Web app, by the signed-in person who sees the
    screenshot; other approvals may also come from a person in an MCP client."""
    if via not in APPROVAL_CHANNELS:
        raise Rejected("invalid_channel")
    if payment and via != "web":
        raise Rejected("web_approval_required", "payments are approved on the Web app only")


# ---------------------------------------------------------------- grants

# A grant approves at most this many inputs, for at most this long.
GRANT_MAX_USES = 20
GRANT_TTL = timedelta(hours=1)
# Terms a covered input must share with the approved one: a message goes to the same
# person from the same account (its text may differ).
GRANT_TERMS = ("sender", "recipient")


def _one_by_one(policy: Policy) -> str | None:
    """Why an input held by this policy is approved only on its own, or None."""
    risk = policy.risk
    effects = set(policy.declared_effects) | set(risk.effects if risk else [])
    if risk is not None and risk.failed:
        return "the risk check could not assess it"
    if is_payment(policy.declared_effects, risk) or "purchase" in effects:
        return "payments and orders are approved one by one"
    if policy.found_rank is not None:
        return "an app a search found is confirmed each time"
    if set(policy.text_flags) & MONEY_FLAGS:
        return "typed money or card numbers are approved one by one"
    if policy.app_sensitive:
        return "inputs in a sensitive app are approved one by one"
    if risk is not None and risk.sensitive and not set(risk.effects) & SENSITIVE_EFFECTS:
        return "the risk check could not say what it does"
    if not effects:
        return "it has no effect to approve"
    return None


def grant_scope(action: Action) -> tuple[list[str], dict[str, str]]:
    """The effects and terms a grant made from this held input covers. Refuses inputs
    that are approved one by one: payments and orders (always, AGENTS.md), an app a
    search found, typed money or card numbers, inputs in a sensitive app, and inputs the
    risk check could not assess or could not explain."""
    if action.policy is None:
        raise Rejected("grant_not_allowed", "no recorded policy")
    reason = _one_by_one(action.policy)
    if reason:
        raise Rejected("grant_not_allowed", reason)
    risk = action.policy.risk
    effects = sorted(set(action.policy.declared_effects) | set(risk.effects if risk else []))
    terms = {f: v for f, v in (action.intent or {}).items() if f in GRANT_TERMS and v}
    return effects, terms


def grant_status(grant: Grant, *, control_revision: int, now: datetime) -> str:
    """active, or why the grant approves nothing more: revoked, ended (the task was
    paused, stopped or resumed since), used_up or expired."""
    if grant.revoked_at is not None:
        return "revoked"
    if grant.control_revision != control_revision:
        return "ended"
    if grant.uses >= grant.max_uses:
        return "used_up"
    if now >= grant.expires_at:
        return "expired"
    return "active"


def _same_party(a: str, b: str) -> bool:
    """The same person or account, written the same way (unlike _same_term, a name that
    merely contains the other is not enough: "Alice" is not "Alice Wong")."""
    norm_a, norm_b = (re.sub(r"[\W_]+", "", t.casefold()) for t in (a, b))
    return bool(norm_a) and norm_a == norm_b


def grant_covers(
    grant: Grant,
    *,
    policy: Policy,
    device_id: UUID,
    app: str,
    terms: Mapping[str, str] | None,
    retry: bool,
    control_revision: int,
    now: datetime,
) -> bool:
    """Whether the grant approves this held input: the grant is active; same device and
    app; the input's sensitive effects are among the grant's (or, for a grant without
    any, the input has none either); the same recipient and sender; and nothing about
    it is approved one by one. A retry of an action confirmed without effect is not
    covered: the person decides whether to try again."""
    if grant_status(grant, control_revision=control_revision, now=now) != "active":
        return False
    if grant.device_id != device_id or grant.app != app or retry:
        return False
    if _one_by_one(policy) is not None:
        return False
    risk = policy.risk
    sensitive = (set(policy.declared_effects) | set(risk.effects if risk else [])) & (
        SENSITIVE_EFFECTS
    )
    granted = set(grant.effects) & SENSITIVE_EFFECTS
    if not sensitive <= granted or (granted and not sensitive):
        return False
    for field in GRANT_TERMS:
        mine, theirs = (terms or {}).get(field), grant.terms.get(field)
        if (mine or theirs) and not (mine and theirs and _same_party(mine, theirs)):
            return False
    return True


# ---------------------------------------------------------------- purchase intents (§10.4)

# Effects that submit an order or pay: in a task with several phones, only the phone the
# person chose may cause them, on the terms they chose.
ORDER_EFFECTS = frozenset({"purchase", "payment"})
PURCHASE_OPTIONS_MAX = 10
PURCHASE_TERMS = ("payee", "amount", "currency")


def check_purchase_options(options: Sequence[PurchaseOption], device_ids: Collection[UUID]) -> None:
    """What the agent proposes: one to PURCHASE_OPTIONS_MAX options, each on a phone of
    the task, with a payee, an amount and a currency."""
    if not 1 <= len(options) <= PURCHASE_OPTIONS_MAX:
        raise Rejected("invalid_arguments", f"propose 1 to {PURCHASE_OPTIONS_MAX} options")
    for option in options:
        if option.device_id not in device_ids:
            raise Rejected("invalid_arguments", "every option is on a phone of this task")
        if not all(getattr(option, f).strip() for f in PURCHASE_TERMS):
            raise Rejected("invalid_arguments", "every option needs payee, amount and currency")
        try:
            Decimal(re.sub(r"[^\d.\-]", "", option.amount))
        except InvalidOperation:
            raise Rejected("invalid_arguments", "amount is a number, as shown") from None


def check_new_purchase(*, pending: bool) -> None:
    """A purchase may be proposed only while no order or payment the task submitted has
    a pending or unknown result: until it is settled, nothing else may be ordered."""
    if pending:
        raise Rejected(
            "purchase_pending",
            "an order or payment has a pending or unknown result; settle it first",
        )


def check_purchase(
    *,
    effects: Iterable[str],
    phones: int,
    intent: PurchaseIntent | None,
    device_id: UUID,
    terms: Mapping[str, str] | None,
) -> UUID | None:
    """The purchase an order or payment input submits for, given the task's newest
    purchase intent. None when it needs none: no order or payment, or a task with one
    phone (decision D10 applies as is). Otherwise the person must have chosen an option
    of the newest intent (``purchase_choice_required``), on this phone
    (``purchase_elsewhere``), with the same payee, amount and currency
    (``purchase_changed``: a new price or shop is proposed again)."""
    if not set(effects) & ORDER_EFFECTS or phones < 2:
        return None
    if intent is None or intent.status != "chosen" or intent.chosen is None:
        raise Rejected(
            "purchase_choice_required",
            "this task uses several phones: propose where to buy with propose_purchase "
            "and wait for the user's choice",
        )
    option = intent.options[intent.chosen]
    if option.device_id != device_id:
        raise Rejected("purchase_elsewhere", "the user chose to buy on another phone")
    for field in PURCHASE_TERMS:
        chosen, stated = str(getattr(option, field)), str((terms or {}).get(field) or "")
        if not stated or not _same_term(field, chosen, stated):
            raise Rejected(
                "purchase_changed",
                f"{field}: the user chose {chosen!r} but this input has {stated!r}; "
                "propose the purchase again",
            )
    return intent.id


def check_purchase_choice(intent: PurchaseIntent, *, newest: UUID, option: int | None) -> None:
    """A person chooses an option of the task's newest proposal, or none of them, once."""
    if intent.status != "proposed":
        raise Rejected("already_decided")
    if intent.id != newest:
        raise Rejected("superseded", "the agent proposed again since")
    if option is not None and not 0 <= option < len(intent.options):
        raise Rejected("invalid_option")


GROUP_NAME_MAX = 80


def group_name(name: str) -> str:
    """A task group's name: trimmed, not empty, at most GROUP_NAME_MAX characters."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise Rejected("invalid_group_name", "a group needs a name")
    if len(cleaned) > GROUP_NAME_MAX:
        raise Rejected("invalid_group_name", f"at most {GROUP_NAME_MAX} characters")
    return cleaned


AGENT_NAME_MAX = 40


def agent_name(raw: object) -> str | None:
    """A sub-agent's name as the delegating agent gave it: one line, at most
    AGENT_NAME_MAX characters (cut, not refused: a name is a label, not an order).
    Nothing usable is no name."""
    if not isinstance(raw, str):
        return None
    cleaned = " ".join(raw.split())[:AGENT_NAME_MAX].strip()
    return cleaned or None


# Citation marks a model with web search leaves in its text (private-use characters).
_CITATION_MARKS = re.compile("\ue200[^\ue201]*\ue201|[\ue200-\ue2ff]")


def shown_text(text: str) -> str:
    """Model text as a person reads it: without the provider's citation marks."""
    return _CITATION_MARKS.sub("", text).strip()


# The answers an agent may offer with a question: a few short ones.
ASK_OPTIONS_MIN = 2
ASK_OPTIONS_MAX = 4
ASK_OPTION_CHARS = 120


def queued_input(
    *, kind: str, run_id: object, seq: int, read_through: int, seen: Collection[int], live: bool
) -> bool:
    """Whether the user's message to the main agent still waits for it to read: no
    step of the main agent has read it, nor a side turn, and the task goes on."""
    return live and kind == "note" and run_id is None and seq > read_through and seq not in seen


# Why a run that waits for the user stopped, when it did not ask a question: the user
# decides how it goes on, so the main agent cannot wake it. "unconfirmed" (a
# completion the check of D37 kept contradicting) is no longer written, but runs that
# stopped for it before still wait for their user.
STOPPED_FOR_USER = ("unconfirmed", "plan", "repeating")


def asked_question(wait_detail: str | None) -> bool:
    """Whether a run waiting for the user waits for the answer to its question, not
    because it stopped for the user to decide (a model error, a loop, a disagreement)."""
    detail = wait_detail or ""
    return detail not in STOPPED_FOR_USER and not detail.startswith("model error:")


# A step that ends without being applied (its worker died, or it raised) is taken again
# from stored state, and each try calls, and pays for, the model. After this many in a
# row the run waits for its user instead.
UNFINISHED_STEPS_MAX = 3


def steps_keep_failing(latest: Sequence[str]) -> bool:
    """Whether a run's latest steps (their statuses, newest first) ended unfinished this
    many times in a row: the newest was just left ``sent``, the ones before abandoned."""
    return (
        len(latest) >= UNFINISHED_STEPS_MAX
        and latest[0] == "sent"
        and all(s == "abandoned" for s in latest[1:UNFINISHED_STEPS_MAX])
    )


@dataclass(frozen=True)
class TurnEnd:
    """Where a run goes when its turn ends (``turn_end``)."""

    status: RunStatus  # COMPLETED, WAITING or RUNNABLE
    # Why it waits; None while it waits for its open tool calls, which say why themselves.
    wait_reason: WaitReason | None = None
    detail: str | None = None
    notice: Literal["run.repeating"] | None = None
    question: str | None = None  # told to the user, or to the main agent with ``to_main``
    to_main: bool = False  # the main agent answers in a side turn
    asked: bool = False  # the question is the agent's own: its options go with it


def turn_end(
    *,
    completed: bool,
    plan: bool,
    repeating: bool,
    open_calls: bool,
    goal_continue: bool,
    question: str | None,
    ask_main: bool,
    called: bool,
    reply: str,
    searched: bool,
    in_circles: bool,
) -> TurnEnd:
    """Where a run goes when its turn ends; the first that applies wins. A completion
    ends it. A plan to approve or a loop stops it for the user. Open tool calls hold it
    until they resolve. A turn that only searched the web goes on, unless it searched
    in circles; so does one whose goal an independent check found unmet. A sub-agent's
    question to the main agent waits for a side turn. A question, or a reply
    without tools, waits for the user: it ends the agent's turn, not the task. Anything
    else goes on."""
    waiting, user = RunStatus.WAITING, WaitReason.USER_INPUT
    if completed:
        return TurnEnd(RunStatus.COMPLETED)
    if plan:
        return TurnEnd(waiting, user, "plan")
    if repeating:
        return TurnEnd(waiting, user, "repeating", notice="run.repeating")
    if open_calls:
        return TurnEnd(waiting)
    if question is None and not called and searched and not in_circles:
        return TurnEnd(RunStatus.RUNNABLE)
    if goal_continue:
        return TurnEnd(RunStatus.RUNNABLE)
    if ask_main and question:
        return TurnEnd(
            waiting, WaitReason.AGENT, question[:1000], question=question, to_main=True, asked=True
        )
    if question is not None or not called:
        # OpenAI requests require a tool call; other providers may still reply without one.
        text = (
            question
            or reply
            or (
                "I searched the web several times without getting anywhere. Tell me how to go on."
                if searched
                else "The agent is waiting for you."
            )
        )
        return TurnEnd(waiting, user, text[:1000], question=text, asked=question is not None)
    return TurnEnd(RunStatus.RUNNABLE)


def ask_options(options: object, recommended: object) -> tuple[list[str], int | None]:
    """The options an agent offers with its question, and the index of the one it
    recommends: at most ``ASK_OPTIONS_MAX`` short, distinct answers. Fewer than two are
    no choice, so none are kept; a recommendation that is not one of them is dropped."""
    if not isinstance(options, list):
        return [], None
    kept: list[str] = []
    for option in options:
        text = shown_text(option)[:ASK_OPTION_CHARS] if isinstance(option, str) else ""
        if text and text not in kept:
            kept.append(text)
    kept = kept[:ASK_OPTIONS_MAX]
    if len(kept) < ASK_OPTIONS_MIN:
        return [], None
    if isinstance(recommended, bool) or not isinstance(recommended, int):
        return kept, None
    return kept, recommended if 0 <= recommended < len(kept) else None


# A scheduled run may start this late (devices busy, worker down); later it is skipped.
SCHEDULE_GRACE = timedelta(minutes=30)
# The worker looks for due schedules at least this often: a schedule made meanwhile, or
# one waiting for a phone, is seen within it.
SCHEDULE_POLL = timedelta(seconds=15)
# A run none of whose phones can be used waits this long for one to come back, then
# fails: as long as a scheduled run may start late.
DEVICE_WAIT = SCHEDULE_GRACE


def usable(device: Device) -> bool:
    """Whether a run can use the device now: its Gateway is connected and reports nothing
    missing (a phone that is plugged in but does not answer reports ``not_responding``)."""
    return device.online and not device.problems


# What does not keep a person from taking a phone over: its locked or dark screen, which
# the Gateway tries waking for them (a phone without a passcode then unlocks).
TAKEOVER_TOLERATES = frozenset({"locked"})


def require_usable(device: Device, *, takeover: bool = False) -> None:
    """Raises unless the device is usable: its screen is asked for (the agent's screenshot,
    a person watching the task or taking the phone over) only then. An offline phone
    (``device_offline``) or one that reports a problem (``device_not_ready``, e.g. not
    responding) would send no picture: asking it would only wait for nothing. A person
    taking it over may find it locked: the takeover wakes it, and says so if it cannot."""
    if not device.online:
        raise Rejected("device_offline")
    problems = [p for p in device.problems if not (takeover and p in TAKEOVER_TOLERATES)]
    if problems:
        raise Rejected("device_not_ready", f"{device.name}: {', '.join(problems)}")


def device_wait(devices: Sequence[Device]) -> str | None:
    """What a run waits for when it holds devices and none of them can be used: its wait
    detail, naming them. None when it can go on: without devices, or with the usable ones
    (its tools tell the agent which of the others it cannot use now)."""
    if not devices or any(usable(d) for d in devices):
        return None
    return f"waiting for {', '.join(d.name for d in devices)} to reconnect"


def device_wait_ran_out(run: Run, waiting: str) -> bool:
    """Whether a run, claimed while none of its devices can be used, has waited for these
    same devices for DEVICE_WAIT. A device that comes back wakes the run with its wait
    cleared (``runs.wake_for_devices``), so a run still marked as waiting for them was
    claimed at the wake-up time set DEVICE_WAIT ahead when it began to wait."""
    return run.wait_reason == WaitReason.DEVICE and run.wait_detail == waiting


def device_wait_failed(devices: Sequence[Device]) -> str:
    """The run's summary when it gave up waiting for its devices."""
    why = {p.replace("_", " ") for d in devices for p in d.problems}
    if any(not d.online for d in devices):
        why.add("offline")
    minutes = int(DEVICE_WAIT.total_seconds() // 60)
    return (
        f"{', '.join(d.name for d in devices)} could not be used for {minutes} minutes "
        f"({', '.join(sorted(why))}); reconnect it and send a message to go on."
    )


_SEARCH_DAYS = 400


def check_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Rejected("unknown_timezone", f"unknown time zone {name!r}") from None


def check_schedule(
    *,
    repeat: Repeat,
    timezone: str,
    weekdays: Sequence[int],
    month_day: int | None,
    run_on: date | None,
) -> None:
    """What each kind of repeat needs."""
    check_timezone(timezone)
    if repeat == Repeat.WEEKLY and (not weekdays or any(not 0 <= d <= 6 for d in weekdays)):
        raise Rejected("invalid_schedule", "a weekly schedule needs days 0 (Monday) to 6")
    if repeat == Repeat.MONTHLY and not (month_day and 1 <= month_day <= 31):
        raise Rejected("invalid_schedule", "a monthly schedule needs a day from 1 to 31")
    if repeat == Repeat.ONCE and run_on is None:
        raise Rejected("invalid_schedule", "a one-time schedule needs a date")


def next_run(
    *,
    repeat: Repeat,
    time_of_day: time,
    timezone: str,
    weekdays: Sequence[int],
    month_day: int | None,
    run_on: date | None,
    after: datetime,
) -> datetime | None:
    """The first moment strictly after ``after`` when the schedule runs, in UTC; None if
    it never will (a one-time schedule whose moment has passed). Times are local to
    ``timezone``, so "every day at 08:00" stays 08:00 across daylight-saving changes."""
    zone = check_timezone(timezone)
    if repeat == Repeat.ONCE:
        assert run_on is not None
        moment = datetime.combine(run_on, time_of_day, zone).astimezone(UTC)
        return moment if moment > after else None
    day = after.astimezone(zone).date()
    for _ in range(_SEARCH_DAYS):
        if _runs_on(repeat, day, weekdays, month_day):
            moment = datetime.combine(day, time_of_day, zone).astimezone(UTC)
            if moment > after:
                return moment
        day += timedelta(days=1)
    return None


def schedule_wait(soonest: datetime | None, now: datetime) -> float:
    """Seconds until the worker looks for due schedules again: until the soonest one
    coming, so it starts on time, but never longer than SCHEDULE_POLL."""
    cap = SCHEDULE_POLL.total_seconds()
    if soonest is None:
        return cap
    return min(max((soonest - now).total_seconds(), 0.0), cap)


def _runs_on(repeat: Repeat, day: date, weekdays: Sequence[int], month_day: int | None) -> bool:
    if repeat == Repeat.DAILY:
        return True
    if repeat == Repeat.WEEKDAYS:
        return day.weekday() < 5
    if repeat == Repeat.WEEKLY:
        return day.weekday() in weekdays
    assert month_day is not None
    last = calendar.monthrange(day.year, day.month)[1]
    return day.day == min(month_day, last)


# The fast risk check: the Decisions API's probabilities are taken only when it
# is sure nothing sensitive happens; anything else goes to the full check.
FAST_CLEAR = 0.1
FAST_CHANGE = 0.5
CONSENT = "consent"  # grants a permission or accepts terms, a consent or tracking prompt


def fast_verdict(probabilities: Mapping[str, float | None]) -> RiskVerdict | None:
    """A verdict from the fast check, or None when the full check must decide: when any
    sensitive effect or consent is at least FAST_CLEAR likely, or a probability is
    missing (a refusal, an error)."""

    def p(name: str) -> float:
        value = probabilities.get(name)
        return 1.0 if value is None else value

    if any(p(e) >= FAST_CLEAR for e in SENSITIVE_EFFECTS) or p(CONSENT) >= FAST_CLEAR:
        return None
    effects = [CHANGE_EFFECT] if p(CHANGE_EFFECT) >= FAST_CHANGE else []
    return RiskVerdict(
        sensitive=False, effects=effects, reason="fast check: nothing sensitive", seen={}
    )


# How hard the agent may think; never below medium (lower effort misjudged inputs).
# How hard a model thinks, least first. A model starts at LOWEST_EFFORT unless the
# deployment lets it go lower. The deployment's default, which the risk check also
# uses, never goes below it: lower efforts were no faster for the risk check and misjudged
# ordinary inputs.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
LOWEST_EFFORT = "medium"


def efforts_from(lowest: str) -> tuple[str, ...]:
    """The efforts of a model whose lowest is ``lowest``, least first."""
    return REASONING_EFFORTS[REASONING_EFFORTS.index(lowest) :]


def check_reasoning_effort(effort: str, lowest: str = LOWEST_EFFORT) -> None:
    allowed = efforts_from(lowest)
    if effort not in allowed:
        raise Rejected("invalid_effort", f"reasoning effort is one of {', '.join(allowed)}")


def effort_on_model(effort: str, lowest: str) -> str:
    """The effort a task keeps when it moves to a model whose lowest is ``lowest``: its
    own, raised to that lowest when it was below it."""
    return effort if effort in efforts_from(lowest) else lowest


TITLE_MAX = 80
# Quotes and sentence endings, Latin and Chinese (written out: they look alike).
_TITLE_QUOTES = "\"' \u201c\u201d\u2018\u2019\u300c\u300d\u300a\u300b"
_TITLE_ENDINGS = ".!?,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a"


def task_title(text: str) -> str | None:
    """A model's title as shown: one line, without quotes or ending punctuation."""
    line = " ".join(text.split())
    line = line.strip(_TITLE_QUOTES).rstrip(_TITLE_ENDINGS).strip(_TITLE_QUOTES)
    return line[:TITLE_MAX] or None
