"""Going in circles: the same inputs or searches over and over."""

from collections.abc import Mapping, Sequence
from typing import Any

from witx.domain.rules.common import Rejected
from witx.domain.rules.inputs import POINT_KINDS

# A cycle of inputs repeated this often since the user's last message: the agent is warned
# with the input's result, then stopped to ask the user.
LOOP_WARN = 4


LOOP_ASK = 8


# Taps on the same row count as the same input (an agent aims at the same element from
# slightly different x positions); screenshot pixels.
LOOP_Y_TOLERANCE = 40


# Swipes, back and home move around a screen and break a cycle: scrolling is not a loop.
_LOOPING_KINDS = POINT_KINDS | {"type_text", "launch_app"}


def same_input(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
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
    return a.get("app_id") == b.get("app_id") and a.get("query") == b.get("query")


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
        if len(last) < size or all(same_input(last[0], other) for other in last[1:]):
            continue
        count = 1
        while len(inputs) >= size * (count + 1) and all(
            same_input(inputs[-1 - i], inputs[-1 - i - size * count]) for i in range(size)
        ):
            count += 1
        best = max(best, count)
    return best
