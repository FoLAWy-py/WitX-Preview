"""HTTP API between the Web app and the cloud (``/api/v1``)."""

from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from witx_contracts.common import ActionKind, Capability, Platform, Problem, Rotation, Wire

TaskStatus = Literal["active", "pausing", "paused", "stopping", "stopped", "completed", "failed"]
RunStatus = Literal["runnable", "running", "waiting", "completed", "failed", "cancelled"]
# "agent": a sub-agent waits for the main agent's answer to its question.
WaitReason = Literal[
    "observation",
    "action",
    "approval",
    "user_input",
    "children",
    "device",
    "model_retry",
    "paused",
    "agent",
]
ActionStatus = Literal[
    "awaiting_approval", "queued", "sent", "done", "rejected", "denied", "cancelled"
]
Effect = Literal["not_attempted", "unverified", "unknown", "success", "no_effect"]
# What an input may do; all but change_data are sensitive.
EffectName = Literal[
    "purchase",
    "payment",
    "send_message",
    "share_data",
    "account_change",
    "delete_data",
    "change_data",
]
# "agent": the main agent's word to a sub-agent, written by no person; "system": a
# note the system writes on the person's behalf, which they do not withdraw.
InputKind = Literal["note", "goal", "constraint", "agent", "system"]
# When the agent stops to ask the user before a device input:
# ask = input that changes data; auto = sensitive input ("approve for me");
# full = only payments.
PermissionMode = Literal["ask", "auto", "full"]
# How hard the agent thinks; never below medium.
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


# ---------------------------------------------------------------- requests


class TaskSpec(Wire):
    """What a task does, whether it starts now (TaskCreate) or later (ScheduleCreate)."""

    goal: str = Field(min_length=1, max_length=4000)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    device_ids: list[UUID] = Field(default_factory=list, max_length=8)
    model: str | None = Field(default=None, max_length=128)
    permission_mode: PermissionMode = "auto"
    reasoning_effort: ReasoningEffort | None = None  # None: the deployment's default
    # IANA name, e.g. Asia/Shanghai: the person's, for "tomorrow" and proposed schedules
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


class TaskCreate(TaskSpec):
    group_id: UUID | None = None  # filed in this group from the start (a schedule's are not)


Repeat = Literal["once", "daily", "weekdays", "weekly", "monthly"]


class ScheduleCreate(TaskSpec):
    """A task that starts by itself: what TaskSpec says, and when."""

    repeat: Repeat
    time_of_day: time  # local time in `timezone`
    timezone: str = Field(min_length=1, max_length=64)  # IANA name, e.g. Asia/Shanghai
    weekdays: list[int] = Field(default_factory=list, max_length=7)  # weekly: 0 = Monday
    month_day: int | None = Field(default=None, ge=1, le=31)  # monthly
    run_on: date | None = None  # once


class ScheduleView(Wire):
    id: UUID
    goal: str
    constraints: list[str]
    device_ids: list[UUID]
    permission_mode: PermissionMode
    model: str
    reasoning_effort: ReasoningEffort
    repeat: Repeat
    time_of_day: time
    timezone: str
    weekdays: list[int]
    month_day: int | None
    run_on: date | None
    enabled: bool
    next_run_at: datetime | None  # None: it will not run again
    last_run_at: datetime | None
    last_task_id: UUID | None
    last_outcome: str | None  # started, waiting:<why> or skipped:<why>


class ScheduleUpdate(Wire):
    enabled: bool


class PermissionModeUpdate(Wire):
    permission_mode: PermissionMode


class TenantSettings(Wire):
    """Tenant-wide settings. Only an owner may change them; a setting left out (null)
    stays as it is."""

    allow_full_access: bool | None = None
    # The risk check tries OpenAI's Decisions API first.
    fast_risk_check: bool | None = None
    # The model providers the organisation's screenshots and text may go to.
    allowed_providers: list[str] | None = None
    # A task whose model keeps failing switches to another allowed model.
    model_fallback: bool | None = None


class TaskInputCreate(Wire):
    """A message from the user to the task's agent: the main agent, or with ``run_id``
    the agent whose question it answers."""

    text: str = Field(min_length=1, max_length=4000)
    run_id: UUID | None = None


class ApprovalDecision(Wire):
    decision: Literal["approve", "deny"]
    # Approve later inputs of the same kind too (a grant, spec §10.4). Not for payments
    # or orders, which are always approved one by one.
    similar: bool = False


class PurchaseChoice(Wire):
    """A person's choice among the purchase options; null chooses none."""

    option: int | None = Field(default=None, ge=0)


class Reconcile(Wire):
    """A user's ruling on an action whose effect is unknown, backed by a newer observation."""

    effect: Literal["success", "no_effect"]
    observation_id: UUID


# ---------------------------------------------------------------- views


class Me(Wire):
    user_id: UUID
    display_name: str
    tenant_id: UUID
    tenant_name: str
    role: Literal["owner", "member"]
    allow_full_access: bool
    fast_risk_check: bool
    allowed_providers: list[str]  # where the organisation's data may go
    model_fallback: bool
    auto_memory: bool = True  # agents keep what they learn for this person
    # Retention: screenshots of this person's finished tasks, and the summary
    # a deleted task leaves, are kept this many days. The person chooses the first, up to
    # the deployment's most.
    screenshot_retention_days: int
    screenshot_retention_max: int
    audit_retention_days: int


class DeviceView(Wire):
    id: UUID
    name: str
    platform: Platform
    online: bool
    capabilities: list[Capability]
    task_id: UUID | None
    task_goal: str | None  # the goal of that task, to show what the device works on
    pool: str  # the Gateway the device is connected to
    problems: list[Problem]  # empty: nothing missing on the Gateway's side
    enabled: bool  # an owner enabled it; until then it takes no task
    taken_over: bool = False  # a person controls it right now (in a task or from the pool)


GatewayState = Literal["waiting", "enrolled", "disabled", "revoked"]


class GatewayView(Wire):
    """A device pool: one Gateway and the devices it drives."""

    id: UUID
    name: str
    host: str | None  # the machine it enrolled from
    state: GatewayState  # waiting: the enrollment code has not been used yet
    connected: bool
    devices: int
    last_seen_at: datetime | None
    enroll_expires_at: datetime | None


class GatewayCreate(Wire):
    name: str = Field(min_length=1, max_length=128)


class GatewayCreated(Wire):
    """Shown once: the code is not stored, only its hash."""

    gateway: GatewayView
    code: str


class DeviceEnable(Wire):
    """Enable a device for tasks (owners only), optionally under a new name."""

    name: str | None = Field(default=None, max_length=128)


class DeviceChange(Wire):
    change: Literal["enabled", "disabled", "taken_over"]
    name: str
    by: str
    at: datetime


class TaskSummary(Wire):
    id: UUID
    request: str  # the first message
    goal: str  # empty: none (most tasks); set for complex or long work
    title: str | None  # a few words from the first message; None until made
    status: TaskStatus
    permission_mode: PermissionMode
    needs_attention: bool
    group_id: UUID | None
    pinned: bool  # the signed-in person pinned it
    created_at: datetime
    updated_at: datetime


class TaskGroupView(Wire):
    id: UUID
    name: str
    created_at: datetime


class TaskGroupName(Wire):
    name: str = Field(min_length=1, max_length=200)


class TaskGroupMove(Wire):
    group_id: UUID | None


class TaskInputView(Wire):
    seq: int
    kind: InputKind
    text: str
    created_at: datetime
    run_id: UUID | None = None  # the agent it went to; none: the main agent
    # Not read by the main agent yet: it reads it at its next step, or now when the
    # person steers; until then it can be withdrawn.
    queued: bool = False
    created_by: UUID | None = None  # who wrote it; none for the main agent's word


class RunView(Wire):
    id: UUID
    parent_run_id: UUID | None
    goal: str
    status: RunStatus
    wait_reason: WaitReason | None
    wait_detail: str | None
    device_ids: list[UUID]
    created_at: datetime
    name: str | None = None  # a sub-agent's name, given when it was delegated to
    # What a finished run ended with: a round of the task (a root run), whose result
    # stays in the conversation after the task goes on, or a sub-agent.
    result: "TaskResult | None" = None
    # What the agent is saying right now: its words as the model produces them,
    # while a step runs; nothing between steps.
    saying: str | None = None
    phase: "Phase | None" = None  # what its step is at


# What a run's step is at: writing its message, planning its next input, having
# its risk assessed, having its goal judged, or working on after its completion
# was refused (its words stay up until it says something new).
Phase = Literal["talking", "planning", "assessing", "checking", "rejected"]


class SayingView(Wire):
    """What a run is saying right now and what its step is at, sent on the task's
    event stream as ``agent.saying`` while a step runs: the text of its message,
    or the summary or question it is about to finish or ask with. Not an event: it has
    no sequence number, is not kept, and an empty ``text`` with no ``phase`` says the
    step is over."""

    run_id: UUID
    text: str
    phase: Phase | None = None


class ApprovalView(Wire):
    status: Literal["pending", "approved", "denied", "used", "expired"]
    expires_at: datetime
    decided_at: datetime | None
    grant_id: UUID | None = None  # approved by this grant, not by a person deciding it


class GrantView(Wire):
    """A standing approval: later inputs like the approved one go ahead without asking."""

    id: UUID
    created_from: UUID  # the action the person approved "and the same kind"
    device_id: UUID
    app: str
    effects: list[EffectName]
    terms: dict[str, str]  # what must stay the same, e.g. a message's recipient
    status: Literal["active", "revoked", "ended", "used_up", "expired"]
    uses: int
    max_uses: int
    expires_at: datetime
    created_at: datetime


class RiskView(Wire):
    sensitive: bool
    effects: list[EffectName]
    reason: str
    failed: bool
    seen: dict[str, str] | None = None  # a message's or payment's terms on the screenshot


class PolicyView(Wire):
    """Why an input ran at once or waited for approval."""

    mode: PermissionMode
    decision: Literal["allow", "ask"]
    reasons: list[str]
    risk: RiskView | None


class ActionView(Wire):
    id: UUID
    run_id: UUID
    device_id: UUID
    kind: ActionKind
    input: dict[str, Any]
    target: str
    expected_effect: str
    effects: list[EffectName]
    status: ActionStatus
    effect: Effect
    reason: str | None
    observation_id: UUID | None
    post_observation_id: UUID | None
    approval: ApprovalView | None
    policy: PolicyView | None
    # A message's or payment's terms (recipient and text; payee, amount and currency),
    # checked against the screenshot; an approval covers exactly these.
    intent: dict[str, str] | None
    retry_of: UUID | None  # the action, confirmed without effect, that this one tries again
    created_at: datetime
    finished_at: datetime | None = None  # when the phone was done with it
    # What the agent says it changes, in a few words ("plant cherries"); the step's
    # effect label names it.
    changes: str | None = None


class PurchaseOptionView(Wire):
    device_id: UUID
    payee: str
    amount: str
    currency: str
    summary: str
    observation_id: UUID | None


class PurchaseView(Wire):
    """Where the agent proposed to buy, in a task with several phones, and the choice."""

    id: UUID
    goal: str
    options: list[PurchaseOptionView]
    # superseded: the agent proposed again before anyone chose
    status: Literal["proposed", "chosen", "declined", "superseded"]
    chosen: int | None
    decided_at: datetime | None
    created_at: datetime


class ObservationView(Wire):
    id: UUID
    device_id: UUID
    status: Literal["requested", "sent", "complete", "failed"]
    captured_at: datetime | None
    width: int | None
    height: int | None
    foreground_app: str | None


class TaskResult(Wire):
    """How a round ended: completed, or failed (then ``summary`` says why; see the run's
    or task's status)."""

    summary: str
    evidence_observation_ids: list[UUID]
    completed_at: datetime  # when it ended
    # A round the system ended, not the agent: why, so the web can say it in the reader's
    # language (``summary`` has it in English).
    failure: Literal["step_limit"] | None = None
    # The agent offers to keep how it was done as a skill: a button under the summary.
    offer_skill: bool = False


class UsageView(Wire):
    """Provider-reported token usage summed over the task's model calls, independent
    risk checks included (not money)."""

    model_calls: int
    input_tokens: int
    output_tokens: int


class MemoryCreate(Wire):
    text: str = Field(min_length=1, max_length=300)


class RetentionSetting(Wire):
    """How long the screenshots of the person's finished tasks are kept."""

    screenshot_retention_days: int = Field(ge=1, le=30)


class MemorySettings(Wire):
    auto_memory: bool  # agents keep what they learn for this person on their own


class MemoryView(Wire):
    """Something kept for a person's future tasks (§8.2): written by them, or by an
    agent on its own in the task ``source_task_id``."""

    id: UUID
    text: str
    kind: Literal["user", "feedback", "app"] = "user"
    app: str | None = None  # the app an "app" note is about (its package)
    by_agent: bool = False  # an agent kept it on its own; unused while auto memory is off
    source_task_id: UUID | None
    created_at: datetime
    decided_at: datetime | None = None  # when it was kept, or last brought up to date


class SkillView(Wire):
    """A skill the agent can load, from an installed plugin. The person's own,
    written in this WitX, are in the plugin ``my`` and can be edited."""

    name: str  # plugin:skill, as the agent loads it
    plugin: str
    marketplace: str
    version: str  # the plugin's
    description: str
    apps: list[str]
    body: str  # Markdown
    editable: bool
    # "user": loaded only when the user names it in a message ($name), not on the
    # agent's own judgement
    invoke: Literal["auto", "user"] = "auto"


class PluginTrouble(Wire):
    plugin: str
    reason: str  # why its skills are not loaded


class SkillsView(Wire):
    skills: list[SkillView]
    not_loaded: list[PluginTrouble]


class SkillSave(Wire):
    """The person's skill ``my:<name>``: tools and effects its text names in backticks
    are declared for it."""

    description: str = Field(min_length=1, max_length=300)
    apps: list[str] = Field(default_factory=list, max_length=20)
    body: str = Field(min_length=1, max_length=20_000)
    invoke: Literal["auto", "user"] = "auto"


class SkillVersionView(Wire):
    """A saved version of one of the person's skills; saving it again restores it."""

    commit: str
    version: str
    saved_at: datetime
    description: str
    apps: list[str]
    body: str
    invoke: Literal["auto", "user"] = "auto"


class MarketplaceView(Wire):
    """Where plugins are installed from; added and removed on the command line."""

    name: str
    source: str  # a git URL or a local repository


class InstalledPluginView(Wire):
    name: str
    marketplace: str
    version: str
    commit: str


class MarketplacesView(Wire):
    marketplaces: list[MarketplaceView]  # not the person's own (local)
    installed: list[InstalledPluginView]  # not their own skills (my)


class PluginFileView(Wire):
    path: str
    text: str
    installed: str | None  # the installed copy's text; None for a file it does not have


class PluginOfferView(Wire):
    """A plugin a marketplace lists now, to read before installing or updating it
    : installing it is bound to this commit and digest."""

    name: str
    version: str  # "" when it could not be read
    description: str
    skills: list[str]
    source: str
    commit: str
    sha256: str
    trouble: list[str]  # why it cannot be installed
    files: list[PluginFileView]
    removed: list[str]  # files of the installed copy that this one does not have


class PluginInstall(Wire):
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanStepView(Wire):
    step: str
    status: Literal["pending", "in_progress", "done"]


class NoteView(Wire):
    """Something the agent wrote down from a screenshot (D6, #74): true when it was seen."""

    text: str
    run_id: UUID  # the run that wrote it
    observation_id: UUID | None
    seen_at: datetime | None


class EventView(Wire):
    """One entry of a task's ordered event stream (also the SSE payload)."""

    seq: int
    type: str
    data: dict[str, Any]
    created_at: datetime


class EarlierView(Wire):
    """The part of a task its page has not loaded: everything before ``before``."""

    before: datetime


class TaskDetail(Wire):
    id: UUID
    created_by: UUID | None = None  # who started it: their memory is what it uses
    request: str  # the first message
    goal: str  # empty: none (most tasks); set for complex or long work
    goal_paused: bool  # the user paused it: the agent follows the messages meanwhile
    plan_mode: bool  # the user wants a plan first: the agent looks but may not act
    agent_ms: int  # how long the agent worked (model calls, inputs, screenshots)
    goal_ms: int  # of that, while the goal was set and not paused
    working: bool  # something is under way now, so the times above are growing
    title: str | None
    constraints: list[str]
    device_ids: list[UUID]  # the phones the task uses
    status: TaskStatus
    revision: int
    model: str
    reasoning_effort: ReasoningEffort
    permission_mode: PermissionMode
    usage: UsageView
    result: TaskResult | None
    inputs: list[TaskInputView]
    runs: list[RunView]
    actions: list[ActionView]
    grants: list[GrantView]
    purchases: list[PurchaseView]
    # The agent's plan for long work (the root run's), and what it wrote down.
    plan: list[PlanStepView]
    notes: list[NoteView]
    memories: list[MemoryView]  # what this task's agent proposed to remember
    observations: list[ObservationView]
    last_event_seq: int
    created_at: datetime
    updated_at: datetime
    # Long tasks come in parts: actions, observations and notes hold the part from
    # ``earlier.before`` on, with every action not finished or of unknown effect and the
    # screenshots those and the part's results show; None when that is the whole task.
    earlier: EarlierView | None = None
    events: list[EventView] = Field(default_factory=list)  # asked for by type: the part's


class TaskChanges(TaskDetail):
    """What changed in a task after the event ``since``. The task's own fields and its
    short lists are whole, as in ``TaskDetail``; ``actions``, ``observations`` and
    ``runs`` hold only those that the events after ``since`` name and those not finished
    yet, and ``notes`` only the notes of those runs, all of them. ``last_event_seq`` is
    where the next one starts. ``earlier`` and ``events`` are left empty: the page keeps
    its own part, and events come on the stream."""

    since: int


class TaskEarlier(Wire):
    """The part of a task before ``before``, by the same rule as the first: its
    actions, screenshots, notes and the events of the types asked for, and whether an
    earlier part remains."""

    actions: list[ActionView]
    observations: list[ObservationView]
    notes: list[NoteView]
    events: list[EventView]
    earlier: EarlierView | None


# ---------------------------------------------------------------- takeover


class TakeoverStart(Wire):
    device_id: UUID


class SideChatAsk(Wire):
    """A question about the task in a side chat; `thread_id` is the tab."""

    thread_id: UUID
    text: str = Field(min_length=1, max_length=4000)


class SideMessageView(Wire):
    """A question and, once the worker answered it, the answer (or why there is none)."""

    id: UUID
    thread_id: UUID
    question: str
    answer: str | None
    error: str | None
    created_at: datetime
    answered_at: datetime | None


class TakeoverView(Wire):
    takeover_id: UUID
    device_id: UUID
    expires_at: datetime


class TakeoverEnd(Wire):
    """``resume``: hand the device back to the agent; otherwise the task stays paused."""

    resume: bool = False


# WebSocket /api/v1/takeovers/{takeover_id}/connect. The browser first authenticates,
# then receives frames and sends input; any side may end it.


class TakeoverAuth(Wire):
    type: Literal["auth"] = "auth"
    token: str = Field(min_length=1, max_length=8192)
    tenant_id: UUID | None = None


class TakeoverInput(Wire):
    """A tap, long_press, swipe, type_text, back, home or a side key (volume_up,
    volume_down, lock). Coordinates are pixels of ``frame_id``. ``client_seq`` grows with
    each input; ``input_id`` makes a resend harmless."""

    type: Literal["input"] = "input"
    input_id: UUID
    client_seq: int = Field(ge=1)
    frame_id: int = Field(ge=1)
    input: dict[str, Any]


class TakeoverKeepAlive(Wire):
    """The person is still there: the takeover's idle time starts again, no input runs."""

    type: Literal["keep"] = "keep"


class TakeoverFinish(Wire):
    type: Literal["end"] = "end"
    resume: bool = False


class LiveFrame(Wire):
    """The screen of a device working on a task, right now (not stored)."""

    width: int
    height: int
    jpeg: str  # base64
    captured_at: datetime


class TakeoverFrame(Wire):
    """A picture of the screen. ``width``/``height``/``rotation`` are its geometry: a
    gesture is sent against the newest frame, and the browser checks that the one it was
    begun on has the same (a phone turned round keeps its size: hence the rotation)."""

    type: Literal["frame"] = "frame"
    frame_id: int
    width: int
    height: int
    rotation: Rotation
    jpeg: str  # base64


class TakeoverInputResult(Wire):
    type: Literal["input_result"] = "input_result"
    input_id: UUID
    outcome: Literal["rejected", "returned", "unknown"]
    reason: str | None = None


class TakeoverEnded(Wire):
    """Sent once when the takeover is over: ended, timeout, disconnected, stopped, ..."""

    type: Literal["ended"] = "ended"
    reason: str


# ---------------------------------------------------------------- sign-in


class AuthMethods(Wire):
    """How the Web app can sign in: OIDC (a redirect) and/or developer tokens."""

    oidc: bool
    dev: bool


class DevLogin(Wire):
    token: str = Field(min_length=1, max_length=512)


class ModelView(Wire):
    """A model tasks may use (the deployment's catalog)."""

    name: str
    provider: str
    default: bool
    allowed: bool  # its provider is one the tenant allows; others cannot be chosen
    efforts: list[ReasoningEffort]  # what a person may choose for it, least first


class DevicesUpdate(Wire):
    """The phones a task uses. One can join at any time; one leaves only while the task is
    not running (paused or finished)."""

    device_ids: list[UUID] = Field(max_length=8)


class ReasoningEffortUpdate(Wire):
    """How hard the task's agent thinks, from its next step on."""

    reasoning_effort: ReasoningEffort


class ModelUpdate(Wire):
    """Switches a task's model; the agent uses it from its next step on."""

    model: str = Field(min_length=1, max_length=128)
