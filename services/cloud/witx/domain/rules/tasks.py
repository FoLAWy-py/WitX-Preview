"""Tasks: titles, groups, deletion, and a long task's page in parts."""

import re
from bisect import bisect_left
from collections.abc import Sequence
from datetime import datetime

from witx.domain.model import (
    TERMINAL_TASK,
    TaskStatus,
)
from witx.domain.rules.common import Rejected

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


def check_force_stop(status: TaskStatus) -> None:
    """Only a stop already asked for can be forced: the task stops as ever, and only
    the Gateway's confirmation of the barrier is waived."""
    if status != TaskStatus.STOPPING:
        raise Rejected("not_stopping", "only a task that is stopping can be forced to stop")


GROUP_NAME_MAX = 80


def group_name(name: str) -> str:
    """A task group's name: trimmed, not empty, at most GROUP_NAME_MAX characters."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise Rejected("invalid_group_name", "a group needs a name")
    if len(cleaned) > GROUP_NAME_MAX:
        raise Rejected("invalid_group_name", f"at most {GROUP_NAME_MAX} characters")
    return cleaned


TITLE_MAX = 80


# Quotes and sentence endings, Latin and Chinese (written out: they look alike).
_TITLE_QUOTES = "\"' \u201c\u201d\u2018\u2019\u300c\u300d\u300a\u300b"


_TITLE_ENDINGS = ".!?,;:\u3002\uff01\uff1f\uff0c\uff1b\uff1a"


def task_title(text: str) -> str | None:
    """A model's title as shown: one line, without quotes or ending punctuation."""
    line = " ".join(text.split())
    line = line.strip(_TITLE_QUOTES).rstrip(_TITLE_ENDINGS).strip(_TITLE_QUOTES)
    return line[:TITLE_MAX] or None


# The name of a language, as the title model gives it with the title: "English",
# "Simplified Chinese", "Brazilian Portuguese".
_LANGUAGE = re.compile(r"[A-Za-z][A-Za-z ()-]{1,38}[A-Za-z)]")


def task_language(text: str) -> str | None:
    """The language a task's first message is written in, as named in English; None
    for anything that is not plainly such a name, so it is never text to follow."""
    name = " ".join(text.split())
    return name if _LANGUAGE.fullmatch(name) else None
