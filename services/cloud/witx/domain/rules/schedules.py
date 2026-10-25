"""Scheduled tasks: when one runs next."""

import calendar
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from witx.domain.model import (
    Repeat,
)
from witx.domain.rules.common import Rejected

# A scheduled run may start this late (devices busy, worker down); later it is skipped.
SCHEDULE_GRACE = timedelta(minutes=30)


# The worker looks for due schedules at least this often: a schedule made meanwhile, or
# one waiting for a phone, is seen within it.
SCHEDULE_POLL = timedelta(seconds=15)


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
