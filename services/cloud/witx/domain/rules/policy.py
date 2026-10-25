"""Approvals: which inputs need a person's approval, and what an approval covers (D10, D32, D33)."""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from witx.domain.model import (
    AUTO_ASK_EFFECTS,
    CHANGE_EFFECT,
    MONEY_FLAGS,
    PAYMENT_EFFECTS,
    SENSITIVE_EFFECTS,
    Action,
    ActionStatus,
    Approval,
    Grant,
    PermissionMode,
    Policy,
    PurchaseIntent,
    PurchaseOption,
    RiskVerdict,
)
from witx.domain.rules.common import Rejected
from witx.domain.rules.inputs import (
    ASSESSED_KINDS,
    action_digest,
    answered_without_asking,
    permission_prompt,
    same_term,
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
    easy = answered_without_asking(prompt)
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


def needs_approval(action: Action) -> bool:
    if action.policy is None:
        return bool(action.effects)
    return action.policy.decision == "ask"


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
    """The same person or account, written the same way (unlike same_term, a name that
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
        if not stated or not same_term(field, chosen, stated):
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
