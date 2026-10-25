"""Inputs: what may reach a phone, and what an input did there."""

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from witx.domain.model import (
    ALL_INTENT_FIELDS,
    EFFECT_DEFINITIONS,
    INTENT_FIELDS,
    SHOWN_INTENT_FIELDS,
    Action,
    Device,
    Effect,
    Observation,
    ObservationStatus,
    PermissionMode,
    PermissionRequest,
    RiskVerdict,
)
from witx.domain.rules.common import Rejected, canonical_json
from witx.domain.rules.devices import PERSON_ONLY_KINDS, require_usable


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


_ELLIPSIS = ("…", "...", "⋯")


def _whole_part(declared: str, shown: str) -> bool:
    """Whether one is the other or a whole part of it, however spaced: words split them,
    so "美团支付" is part of "美团支付 推荐" but "3号楼" is not part of "13号楼", nor "张三"
    of "张三丰". Only where the screen cuts its text short ("…3号楼") may a word be cut."""
    words = [re.findall(r"[^\W_]+", t.casefold()) for t in (declared, shown)]
    a, b = ("".join(w) for w in words)
    if not a or not b:
        return False
    if len(a) < len(b):  # the screen shows more
        part, whole, cut = a, words[1], ""
    else:  # the screen shows less
        part, whole, cut = b, words[0], shown.strip()
    flat, bounds = "".join(whole), {0}
    for word in whole:
        bounds.add(max(bounds) + len(word))
    return any(
        flat.startswith(part, start)
        and (start in bounds or cut.startswith(_ELLIPSIS))
        and (start + len(part) in bounds or cut.endswith(_ELLIPSIS))
        for start in range(len(flat) - len(part) + 1)
    )


def same_term(field: str, declared: str, shown: str) -> bool:
    """Whether what the screen shows is what the agent declared, allowing for the way
    screens format things: phone numbers, truncated previews, currency signs."""
    if field in ("address", "payment_method"):
        # Screens shorten both ("…3号楼", "美团支付 推荐").
        return _whole_part(declared, shown)
    if field in ("sender", "recipient", "payee"):
        a, b = re.sub(r"\D", "", declared), re.sub(r"\D", "", shown)
        if len(a) >= 7 and len(b) >= 7:
            # The same number, or with a country code before it ("+86 139…").
            short, long = sorted((a, b), key=len)
            return short == long or (
                len(short) >= 10 and len(long) - len(short) <= 3 and long.endswith(short)
            )
        return _whole_part(declared, shown)
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
        elif not same_term(field, terms[field], shown):
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
}
_CARD_NUMBER = re.compile(r"(?<![\d+])(?:\d[ -]?){13,19}(?!\d)")


def has_card_number(text: str) -> bool:
    """A payment card's number: 13 to 19 digits, maybe grouped, whose check digit is
    right (Luhn). A phone number with its country code ("+86 139 …") is not one."""
    for found in _CARD_NUMBER.finditer(text):
        digits = [int(c) for c in reversed(found.group()) if c.isdigit()]
        total = sum(d if i % 2 == 0 else d * 2 - 9 * (d > 4) for i, d in enumerate(digits))
        if total % 10 == 0:
            return True
    return False


def text_flags(payload: Mapping[str, Any]) -> list[str]:
    """Kinds of personal or financial data in text the agent wants to enter."""
    if payload.get("kind") == "type_text":
        text = str(payload.get("text", ""))
    elif payload.get("kind") in ("set_picker", "select_option"):
        text = str(payload.get("value", ""))
    else:
        return []
    found = [name for name, pattern in _TEXT_PATTERNS.items() if pattern.search(text)]
    return sorted([*found, "card_number"] if has_card_number(text) else found)


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


def answered_without_asking(prompt: PermissionRequest | None) -> bool:
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


def found_rank(app_id: str, searches: Sequence[Sequence[Mapping[str, Any]]]) -> int | None:
    """Where ``app_id`` stands in the newest search that found it (0 = best match), or
    None if no search did. ``searches`` lists find_app results, newest first."""
    for found in searches:
        ids = [str(app["app_id"]) for app in found]
        if app_id in ids:
            return ids.index(app_id)
    return None


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
    require_usable(device)
    if not device.enabled:
        raise Rejected("device_not_enabled", "an owner enables devices on the Web app")
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
    "no_focused_field": "Nothing was typed: no field had the focus any more. Tap the field, "
    "check on the new screenshot that it is focused, then type again.",
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
