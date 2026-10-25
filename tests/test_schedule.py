"""Plan 6 slice 1: loop schedules (`every`) and quiet hours (`hours`)."""
from datetime import datetime

import pytest

from pl import schedule


@pytest.mark.parametrize("text", ["30m", "1m", "1440m", "hourly", "daily 09:00", "weekdays 09:30", "cron:*/15 8-18 * * 1-5"])
def test_every_accepts_each_documented_form(text):
    assert schedule.parse_every(text)


@pytest.mark.parametrize("text", ["0m", "1441m", "often", "daily 25:00", "weekdays", "cron:* * *", "cron:61 * * * *", ""])
def test_bad_every_lists_the_accepted_forms(text):
    with pytest.raises(ValueError) as e:
        schedule.parse_every(text)
    for form in schedule.FORMS:
        assert form in str(e.value)


def test_next_run_of_weekdays_skips_the_weekend():
    friday_evening = datetime(2026, 10, 23, 18, 0)
    assert schedule.next_run(schedule.parse_every("weekdays 09:00"), friday_evening) == datetime(2026, 10, 26, 9, 0)


def test_next_run_of_minutes_hourly_and_daily():
    after = datetime(2026, 10, 24, 10, 20)
    assert schedule.next_run(schedule.parse_every("30m"), after) == datetime(2026, 10, 24, 10, 50)
    assert schedule.next_run(schedule.parse_every("hourly"), after) == datetime(2026, 10, 24, 11, 20)
    assert schedule.next_run(schedule.parse_every("daily 09:00"), after) == datetime(2026, 10, 25, 9, 0)


def test_cron_step_and_range_fields():
    spec = schedule.parse_every("cron:*/15 8-9 * * 1,3")
    assert schedule.next_run(spec, datetime(2026, 10, 26, 8, 20)) == datetime(2026, 10, 26, 8, 30)
    assert schedule.next_run(spec, datetime(2026, 10, 26, 9, 50)) == datetime(2026, 10, 28, 8, 0)


def test_quiet_hours_exclude_20_00_on_a_weekday():
    hours = schedule.parse_hours("08:00-18:00 weekdays")
    assert schedule.in_hours(hours, datetime(2026, 10, 23, 9, 0))
    assert not schedule.in_hours(hours, datetime(2026, 10, 23, 20, 0))
    assert not schedule.in_hours(hours, datetime(2026, 10, 24, 9, 0))


def test_bad_hours_are_refused():
    with pytest.raises(ValueError):
        schedule.parse_hours("late")
