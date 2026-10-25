"""The agent's turn: goals, plans, commands, questions, and where a run goes when its turn ends."""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from witx.domain.model import (
    OPEN_ACTION,
    Action,
    Effect,
    Observation,
    ObservationStatus,
    RunStatus,
    WaitReason,
)
from witx.domain.rules.common import Rejected
from witx.domain.rules.inputs import last_inputs

# A completion refused because the goal is not met this many times in a run; the
# next refusal asks the user how to go on.
COMPLETION_REFUSALS = 2


# Turns in a row that end without a tool call while the goal is not met: the agent is not
# getting anywhere, so it stops for the user instead of being sent back again.
GOAL_STALL = 3


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


# Telling a reply's language from its request's, by script: Chinese, Japanese and
# Korean characters against Latin words. Quoted text is left out: a reply may quote what a
# screen shows in any language.
# CJK characters, and the punctuation only CJK text uses (。、，：), which a sentence in
# that language has even when it names English on the screen.
_CJK = re.compile("[\u3000-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff\uff01-\uff1f]")
_LATIN_WORD = re.compile("[A-Za-z\u00c0-\u024f]{2,}")
_QUOTED = re.compile(
    '\u201c[^\u201d]*\u201d|\u300c[^\u300d]*\u300d|\u300e[^\u300f]*\u300f|"[^"]*"|`[^`]*`'
)
# A request is in CJK with this many CJK characters (a name in one, "Text 张三", is not).
# A reply to a request in Latin script with WRONG_LANGUAGE_CJK CJK characters of its own
# is in another language; one to a CJK request with fewer than WRONG_LANGUAGE_CJK_FEW and
# at least WRONG_LANGUAGE_WORDS Latin words is too.
CJK_REQUEST = 4
WRONG_LANGUAGE_CJK = 6
WRONG_LANGUAGE_CJK_FEW = 2
WRONG_LANGUAGE_WORDS = 4


def _script(text: str) -> tuple[int, int]:
    """CJK characters and Latin words outside quotes."""
    own = _QUOTED.sub(" ", text)
    return len(_CJK.findall(own)), len(_LATIN_WORD.findall(own))


def wrong_language(asked: str, reply: str) -> bool:
    """Whether ``reply`` is plainly in another language than ``asked``, the user's newest
    message. Only CJK against Latin script is told apart; anything unclear passes."""
    asked_cjk, asked_words = _script(asked)
    reply_cjk, reply_words = _script(reply)
    if asked_cjk >= CJK_REQUEST and asked_cjk >= asked_words:
        return reply_cjk < WRONG_LANGUAGE_CJK_FEW and reply_words >= WRONG_LANGUAGE_WORDS
    if asked_words >= 2 and asked_cjk < CJK_REQUEST:
        return reply_cjk >= WRONG_LANGUAGE_CJK
    return False


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
