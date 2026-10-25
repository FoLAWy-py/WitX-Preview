"""Domain records and their states. Plain data: no I/O, no framework types."""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID


class PermissionMode(StrEnum):
    """When the Harness stops to ask the user before a device input."""

    ASK = "ask"  # every input waits for the user
    AUTO = "auto"  # ordinary input runs; sensitive input waits ("approve for me")
    FULL = "full"  # everything runs except payments and transfers ("full access")


class TaskStatus(StrEnum):
    ACTIVE = "active"
    PAUSING = "pausing"  # pause requested; waiting for every device barrier
    PAUSED = "paused"  # no model calls or inputs; the devices stay with the task
    STOPPING = "stopping"  # stop requested; waiting for every device barrier
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_TASK = frozenset({TaskStatus.STOPPED, TaskStatus.COMPLETED, TaskStatus.FAILED})


class RunStatus(StrEnum):
    RUNNABLE = "runnable"  # a worker may claim it now
    RUNNING = "running"  # claimed by a worker (owner + epoch)
    WAITING = "waiting"  # see WaitReason; resumed by an external fact
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class WaitReason(StrEnum):
    OBSERVATION = "observation"
    ACTION = "action"
    APPROVAL = "approval"
    USER_INPUT = "user_input"
    CHILDREN = "children"
    DEVICE = "device"
    MODEL_RETRY = "model_retry"
    PAUSED = "paused"  # the task is paused; only resuming it wakes the run
    AGENT = "agent"  # a sub-agent waits for the main agent's answer


class ActionStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"  # admitted; the hub will deliver it
    SENT = "sent"  # delivered to the Gateway; result pending
    DONE = "done"  # the Gateway reported an outcome; see Effect
    REJECTED = "rejected"  # refused before delivery
    DENIED = "denied"  # the user denied approval
    CANCELLED = "cancelled"  # stopped before delivery


OPEN_ACTION = frozenset({ActionStatus.AWAITING_APPROVAL, ActionStatus.QUEUED, ActionStatus.SENT})


class Effect(StrEnum):
    """Business effect, tracked separately from dispatch progress."""

    NOT_ATTEMPTED = "not_attempted"
    UNVERIFIED = "unverified"  # input returned; the next observation shows what happened
    UNKNOWN = "unknown"  # input may have happened; blocks further input until reconciled
    SUCCESS = "success"
    NO_EFFECT = "no_effect"


class ObservationStatus(StrEnum):
    REQUESTED = "requested"
    SENT = "sent"
    COMPLETE = "complete"
    FAILED = "failed"


# What each sensitive effect means. The agent and the independent risk check are given
# these same definitions.
EFFECT_DEFINITIONS = {
    "purchase": "places or confirms an order that is paid for in a later step (a "
    "checkout or cashier screen still follows); an order charged as soon as it is placed "
    "is payment",
    "payment": "pays, transfers or authorises money in any form, including placing an "
    "order that is charged at once (saved card, password-free or one-tap payment)",
    "send_message": "sends a message, comment, post, email or invitation to another person",
    "share_data": "shares personal data, files, contacts or location with someone else",
    "account_change": "changes the user's own account: password, security or sign-in "
    "settings, linked payment methods, or account and delivery addresses",
    "delete_data": "deletes data that cannot easily be restored",
    "change_data": "creates, edits or saves something that stays: a record (note, "
    "reminder, contact, calendar event, draft), a setting, a form it submits, or a choice "
    "it confirms in a game or app (buying with in-game coins, planting, building, "
    "upgrading). Opening apps, navigating, scrolling, searching and typing into a search "
    "box change nothing, and neither do moving around in a game or app, turning the view, "
    "opening or closing a menu, panel or dialog, or selecting something without "
    "confirming it",
}
# What the agent says an input with effects changes, in a few words ("plant cherries"):
# the label of the step in the timeline.
CHANGES_MAX = 40
# Saving ordinary data is not an effect by itself; without this, models took saving a
# contact "to the account" for an account change.
ORDINARY_RECORDS = (
    "Creating or editing an ordinary record in an app, such as a contact, note, "
    "reminder, calendar event or draft, is change_data and none of the sensitive effects "
    "by itself; deleting one is delete_data."
)
# change_data is the one effect that is not sensitive: "ask" mode waits for it (decision
# D10), "auto" lets it run.
CHANGE_EFFECT = "change_data"
SENSITIVE_EFFECTS = frozenset(EFFECT_DEFINITIONS) - {CHANGE_EFFECT}
# Effects that always need the user's approval, whatever the permission mode.
PAYMENT_EFFECTS = frozenset({"payment"})
# What "auto" still asks about: money, and what cannot be taken back. Messages and
# orders (paid for in a later step) go ahead (user decision, D10).
AUTO_ASK_EFFECTS = frozenset({"payment", "delete_data", "share_data", "account_change"})
# Typed text that points at money; a phone number or address in a message does not.
MONEY_FLAGS = frozenset({"money", "card_number"})
# What a message or a payment is about. The agent declares it with the
# input, the risk check reads it from the screenshot, and the approval is bound to it.
INTENT_FIELDS: dict[str, tuple[str, ...]] = {
    "send_message": ("recipient", "body"),
    "payment": ("payee", "amount", "currency"),
    "purchase": ("payee", "amount", "currency"),
}
# Terms that only some screens show: the account a message is sent from; a checkout's
# delivery address and payment method. Stated whenever the screen shows them, and then
# bound like the others.
SHOWN_INTENT_FIELDS: dict[str, tuple[str, ...]] = {
    "send_message": ("sender",),
    "payment": ("address", "payment_method"),
    "purchase": ("address", "payment_method"),
}
ALL_INTENT_FIELDS = (
    "sender",
    "recipient",
    "body",
    "payee",
    "amount",
    "currency",
    "address",
    "payment_method",
)


# What a system permission prompt (the phone's own "Allow ... ?" dialog) asks for, as the
# independent risk check names it.
PERMISSION_CATEGORIES = (
    "location",
    "camera",
    "microphone",
    "photos",
    "contacts",
    "calendar",
    "notifications",
    "bluetooth",
    "local_network",
    "tracking",
    "other",
)


@dataclass(frozen=True)
class PermissionRequest:
    """An input that answers a system permission prompt: ``grants`` says whether it
    allows (else it declines), ``broad`` whether it allows more than the narrowest option
    (always, all photos, full access)."""

    category: str
    grants: bool
    broad: bool = False


@dataclass(frozen=True)
class RiskVerdict:
    """The independent risk assessor's view of one proposed input.

    ``failed`` means no verdict could be obtained; it is treated as sensitive and as a
    possible payment, because neither can be ruled out.
    """

    sensitive: bool
    effects: list[str]
    reason: str
    failed: bool = False
    # What the screenshot shows for a message or payment (see INTENT_FIELDS).
    seen: dict[str, str] | None = None
    # Set when the input answers a system permission prompt.
    permission: PermissionRequest | None = None


@dataclass(frozen=True)
class GoalVerdict:
    """Whether the task's goal, a condition to be met, holds now: "met", "not_met"
    (``reason`` says what is missing, as guidance for the agent) or "impossible" (it can
    never hold; ``reason`` says why)."""

    verdict: Literal["met", "not_met", "impossible"]
    reason: str


@dataclass(frozen=True)
class Policy:
    """Why an input was allowed or held for approval. Stored with the action for audit."""

    mode: PermissionMode
    decision: str  # "allow" | "ask"
    reasons: list[str]
    declared_effects: list[str]
    risk: RiskVerdict | None
    app_sensitive: bool
    text_flags: list[str]
    # launch_app of an app that a search returned: its place in it (0 = best; by query, 0).
    found_rank: int | None = None


@dataclass(frozen=True)
class Task:
    id: UUID
    tenant_id: UUID
    goal: str
    constraints: list[str]
    status: TaskStatus
    revision: int
    control_revision: int
    model: str
    device_ids: list[UUID]
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    permission_mode: PermissionMode = PermissionMode.AUTO
    group_id: UUID | None = None
    reasoning_effort: str = "medium"
    title: str | None = None  # summarised from the first message; None until then
    timezone: str = "UTC"  # the starter's, for "tomorrow" and proposed schedules
    request: str = ""  # the first message; `goal` is set only for complex or long work
    goal_paused: bool = False  # the user paused the goal; the agent follows the messages
    plan_mode: bool = False  # the user wants a plan first: the agent may not act until approval


@dataclass(frozen=True)
class TaskGroup:
    id: UUID
    name: str
    created_at: datetime


class Repeat(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKDAYS = "weekdays"  # Monday to Friday
    WEEKLY = "weekly"  # on the chosen days of the week
    MONTHLY = "monthly"  # on one day of the month (the month's last day if shorter)


@dataclass(frozen=True)
class Schedule:
    """A task that starts by itself: what to do, and when (in a time zone)."""

    id: UUID
    tenant_id: UUID
    created_by: UUID
    goal: str
    constraints: list[str]
    device_ids: list[UUID]
    permission_mode: PermissionMode
    model: str
    repeat: Repeat
    time_of_day: time  # local time in `timezone`
    timezone: str
    weekdays: list[int]  # weekly: 0 = Monday ... 6 = Sunday
    month_day: int | None  # monthly
    run_on: date | None  # once
    enabled: bool
    next_run_at: datetime | None  # None: it will not run again
    last_run_at: datetime | None
    last_task_id: UUID | None
    last_outcome: str | None  # started, or why the run was skipped
    created_at: datetime
    reasoning_effort: str = "medium"


@dataclass(frozen=True)
class Saying:
    """What a run says right now and what its step is at."""

    text: str
    phase: str | None


@dataclass(frozen=True)
class Run:
    id: UUID
    tenant_id: UUID
    task_id: UUID
    parent_run_id: UUID | None
    goal: str
    status: RunStatus
    wait_reason: WaitReason | None
    wait_detail: str | None
    epoch: int
    depth: int
    device_ids: list[UUID]
    result: dict[str, Any] | None
    created_at: datetime
    spawn_key: str | None = None  # "<delegate call ID>:<index>" for a child run
    name: str | None = None  # a sub-agent's name, given when it was delegated to
    saying: str | None = None  # its words as the model produces them, while a step runs
    phase: str | None = None  # what the step is at: talking, planning, assessing, ...


@dataclass(frozen=True)
class Screen:
    width: int
    height: int
    input_width: int
    input_height: int
    rotation: int  # degrees clockwise from the device's natural orientation
    foreground_app: str
    # The sliders in view and the position each reports: name, value, x, y.
    sliders: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Observation:
    id: UUID
    tenant_id: UUID
    device_id: UUID
    task_id: UUID | None
    epoch: int | None
    status: ObservationStatus
    requested_at: datetime
    captured_at: datetime | None = None
    screen: Screen | None = None
    sha256: str | None = None
    object_key: str | None = None


@dataclass(frozen=True)
class Action:
    id: UUID
    tenant_id: UUID
    task_id: UUID
    run_id: UUID
    device_id: UUID
    tool_call_id: UUID
    epoch: int
    control_revision: int
    observation_id: UUID | None
    kind: str
    input: dict[str, Any]
    target: str
    expected_effect: str
    effects: list[str]
    digest: str
    status: ActionStatus
    effect: Effect
    reason: str | None
    post_observation_id: UUID
    created_at: datetime
    sent_at: datetime | None = None
    finished_at: datetime | None = None
    policy: Policy | None = None
    found_apps: list[dict[str, Any]] | None = None  # find_app: matches, best first
    intent: dict[str, str] | None = None  # a message's or payment's terms, approved with it
    reconciled_by: UUID | None = None  # the person who settled an unknown effect
    retry_of: UUID | None = None  # the action, confirmed without effect, this one retries
    purchase_intent_id: UUID | None = None  # the purchase an order or payment submits for
    field_text: str | None = None  # type_text: the field's text read back afterwards
    field_info: dict[str, Any] | None = None  # ... and which field it was: name and box
    changes: str | None = None  # what the agent says it changes, in a few words


@dataclass(frozen=True)
class Approval:
    action_id: UUID
    tenant_id: UUID
    digest: str
    requested_at: datetime
    expires_at: datetime
    decision: str | None = None  # "approve" | "deny"
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    used_at: datetime | None = None
    grant_id: UUID | None = None  # given by this grant, not by a person deciding this input


@dataclass(frozen=True)
class Grant:
    """A person's approval of one held input "and the same kind": later
    inputs of the task on the same device and app, with the same effects and terms (see
    rules.grant_scope), are approved without asking, a limited number of times."""

    id: UUID
    tenant_id: UUID
    task_id: UUID
    created_from: UUID  # the action the person approved
    device_id: UUID
    app: str
    effects: list[str]
    terms: dict[str, str]
    control_revision: int  # the task's when granted; a pause, stop or resume ends it
    max_uses: int
    uses: int
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None


@dataclass(frozen=True)
class PurchaseOption:
    """Where the agent could buy: on which phone, from whom, for how much."""

    device_id: UUID
    payee: str
    amount: str
    currency: str
    summary: str  # what exactly: items, specification, quantity, delivery
    observation_id: UUID | None = None  # the screenshot that shows it


@dataclass(frozen=True)
class PurchaseIntent:
    """One purchase goal in a task that uses several phones. The person
    chooses one option; only that phone may order or pay for it, on its terms."""

    id: UUID
    tenant_id: UUID
    task_id: UUID
    run_id: UUID
    goal: str
    options: list[PurchaseOption]
    status: str  # "proposed" | "chosen" | "declined"
    created_at: datetime
    chosen: int | None = None
    decided_by: UUID | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class Device:
    id: UUID
    tenant_id: UUID
    gateway_id: UUID
    native_id: str
    platform: str
    name: str
    capabilities: list[str]
    online: bool
    epoch: int
    sensitive_apps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # empty: it can take tasks
    enabled: bool = False  # an owner enabled it on the Web app (device pool, #33)


@dataclass(frozen=True)
class Lease:
    """Exclusive input right for one device: one task, one writer run, one epoch. A
    person who took over a device no task holds has a lease without task and run."""

    device_id: UUID
    tenant_id: UUID
    task_id: UUID | None
    run_id: UUID | None
    epoch: int
    stop_id: UUID | None = None
    takeover_id: UUID | None = None  # a person controls the device
    takeover_user: UUID | None = None
    takeover_expires_at: datetime | None = None


@dataclass(frozen=True)
class ToolCall:
    id: UUID
    run_id: UUID
    step_id: UUID
    call_id: str
    name: str
    arguments: dict[str, Any]
    output: dict[str, Any] | None = None
    observation_id: UUID | None = None
    action_id: UUID | None = None
    child_run_id: UUID | None = None


@dataclass(frozen=True)
class Step:
    id: UUID
    run_id: UUID
    seq: int
    status: str  # "sent" | "done" | "error" | "abandoned"
    input_seq: int
    input_items: list[dict[str, Any]]
    output_items: list[dict[str, Any]] = field(default_factory=list)
    goal_check: dict[str, Any] | None = None  # the goal verdict this step's turn ended with


@dataclass(frozen=True)
class Memory:
    """Something kept for a person's future tasks (§8.2): written by them, or by an agent
    on its own. ``kind``: "user" (their preferences), "feedback" (how they corrected
    or confirmed the agent), "app" (how ``app`` works)."""

    id: UUID
    user_id: UUID
    text: str
    status: str  # "kept"; "declined" for proposals from before D59
    source_task_id: UUID | None
    created_at: datetime
    decided_at: datetime | None = None
    kind: str = "user"
    app: str | None = None
    by_agent: bool = False  # kept by an agent on its own


@dataclass(frozen=True)
class RunSummary:
    """A summary of a run's earlier steps: through step ``through_seq``'s input.
    ``summary`` is None when the call that should have written it failed."""

    id: UUID
    run_id: UUID
    through_seq: int
    summary: str | None
    created_at: datetime


@dataclass(frozen=True)
class SideTurn:
    """The main agent's side turn while its sub-agents work: for a sub-agent's
    question (``about_run_id``) or the user's message (``input_seq``)."""

    id: UUID
    tenant_id: UUID
    task_id: UUID
    run_id: UUID
    about_run_id: UUID | None
    input_seq: int | None
    text: str
    created_at: datetime
    done_at: datetime | None = None
    error: str | None = None
    tries: int = 0


@dataclass(frozen=True)
class SideMessage:
    """A question about a task asked in a side chat, and its answer once there is one.
    The answer is only shown: it never acts and never enters the task's conversation."""

    id: UUID
    tenant_id: UUID
    task_id: UUID
    thread_id: UUID
    question: str
    asked_by: UUID
    created_at: datetime
    answer: str | None = None
    error: str | None = None
    tries: int = 0
    answered_at: datetime | None = None
