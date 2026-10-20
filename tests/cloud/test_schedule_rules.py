"""When a scheduled task runs next (pure rules)."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from witx.domain import rules
from witx.domain.model import Repeat
from witx.domain.rules import Rejected

SHANGHAI = "Asia/Shanghai"  # UTC+8, no daylight saving


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def nxt(repeat: Repeat, after: str, **kw) -> datetime | None:  # type: ignore[no-untyped-def]
    return rules.next_run(
        repeat=repeat,
        time_of_day=kw.pop("time_of_day", time(8, 0)),
        timezone=kw.pop("timezone", SHANGHAI),
        weekdays=kw.pop("weekdays", []),
        month_day=kw.pop("month_day", None),
        run_on=kw.pop("run_on", None),
        after=at(after),
    )


def test_a_daily_schedule_runs_next_at_its_local_time() -> None:
    assert nxt(Repeat.DAILY, "2026-10-12T07:59+08:00") == at("2026-10-12T08:00+08:00")
    assert nxt(Repeat.DAILY, "2026-10-12T08:00+08:00") == at("2026-10-13T08:00+08:00")


def test_weekdays_skip_the_weekend() -> None:
    # Friday 2026-10-16 after 08:00 -> Monday 2026-10-19
    assert nxt(Repeat.WEEKDAYS, "2026-10-16T09:00+08:00") == at("2026-10-19T08:00+08:00")


def test_a_weekly_schedule_runs_on_its_chosen_days() -> None:
    # Monday 2026-10-12, chosen Wednesday (2) and Sunday (6)
    assert nxt(Repeat.WEEKLY, "2026-10-12T09:00+08:00", weekdays=[2, 6]) == at(
        "2026-10-14T08:00+08:00"
    )


def test_a_monthly_schedule_on_the_31st_runs_on_a_shorter_month_s_last_day() -> None:
    assert nxt(Repeat.MONTHLY, "2026-11-01T00:00+08:00", month_day=31) == at(
        "2026-11-30T08:00+08:00"
    )


def test_a_one_time_schedule_runs_once() -> None:
    run_on = date(2026, 10, 20)
    assert nxt(Repeat.ONCE, "2026-10-12T00:00+08:00", run_on=run_on) == at("2026-10-20T08:00+08:00")
    assert nxt(Repeat.ONCE, "2026-10-21T00:00+08:00", run_on=run_on) is None


def test_the_local_time_holds_across_daylight_saving() -> None:
    # New York leaves daylight saving on 2026-11-01: 08:00 local is 12:00 UTC before, 13:00 after.
    ny = "America/New_York"
    assert nxt(Repeat.DAILY, "2026-10-31T09:00-04:00", timezone=ny) == at("2026-11-01T13:00Z")


def test_a_schedule_must_say_what_its_repeat_needs() -> None:
    with pytest.raises(Rejected):
        rules.check_schedule(
            repeat=Repeat.WEEKLY, timezone=SHANGHAI, weekdays=[], month_day=None, run_on=None
        )
    with pytest.raises(Rejected):
        rules.check_schedule(
            repeat=Repeat.DAILY, timezone="Mars/Olympus", weekdays=[], month_day=None, run_on=None
        )


def test_the_worker_looks_again_when_the_soonest_schedule_is_due() -> None:
    now = at("2026-10-12T07:59:57+08:00")
    assert rules.schedule_wait(now + timedelta(seconds=3), now) == pytest.approx(3.0)


def test_the_worker_looks_at_least_every_fifteen_seconds() -> None:
    now = at("2026-10-12T07:00+08:00")
    assert rules.schedule_wait(now + timedelta(hours=1), now) == 15.0
    assert rules.schedule_wait(None, now) == 15.0


def test_a_schedule_already_due_is_looked_at_at_once() -> None:
    now = at("2026-10-12T08:00:01+08:00")
    assert rules.schedule_wait(now - timedelta(seconds=1), now) == 0.0
