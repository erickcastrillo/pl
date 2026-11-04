"""Plan 6 slice 1: loop schedules (`every`) and quiet hours (`hours`)."""
from datetime import datetime, timedelta

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


@pytest.mark.parametrize("text,words", [
    ("30m", "every 30 minutes"), ("1m", "every 1 minute"), ("hourly", "every hour"), ("120m", "every 2 hours"),
    ("daily 09:00", "every day at 09:00"), ("weekdays 09:00", "weekdays at 09:00"), ("cron:0 9 * * 1", "custom: 0 9 * * 1"),
])
def test_in_words_for_each_form(text, words):
    assert schedule.in_words(text) == words


def test_in_words_keeps_text_it_cannot_parse():
    assert schedule.in_words("every tuesday") == "every tuesday"
    assert schedule.in_words("") == "always on"


def test_preview_of_weekdays_from_friday_skips_the_weekend():
    runs = schedule.preview("weekdays 09:00", datetime(2026, 10, 30, 10, 0))
    assert runs == [datetime(2026, 11, 2, 9), datetime(2026, 11, 3, 9), datetime(2026, 11, 4, 9)]


@pytest.mark.parametrize("every, check", [
    ("30m", lambda a, b: b - a == timedelta(minutes=30)),
    ("120m", lambda a, b: b - a == timedelta(minutes=120)),
    ("daily 07:30", lambda a, b: (a.hour, a.minute) == (7, 30) and b - a == timedelta(days=1)),
    ("cron:0 9 * * 1", lambda a, b: a.weekday() == 0 and b - a == timedelta(days=7)),
])
def test_preview_of_each_kind(every, check):
    runs = schedule.preview(every, datetime(2026, 10, 26, 12, 0))
    assert len(runs) == 3 and runs == sorted(set(runs))
    assert check(runs[0], runs[1]) and check(runs[1], runs[2])


@pytest.mark.parametrize("every", ["0m", "cron:61 * * * *", "daily 25:00", "cron:0 0 31 2 *"])
def test_preview_refuses_bad_and_never_firing_schedules(every):
    with pytest.raises(ValueError) as e:
        schedule.preview(every, datetime(2026, 10, 26, 12, 0))
    if every == "cron:0 0 31 2 *":
        assert "never fires" in str(e.value)


@pytest.mark.parametrize("values, every", [
    (dict(kind="minutes", number="2", unit="hours"), "120m"),
    (dict(kind="minutes", number="30", unit="minutes"), "30m"),
    (dict(kind="daily", at="07:30"), "daily 07:30"),
    (dict(kind="weekdays", at="09:00"), "weekdays 09:00"),
    (dict(kind="cron", cron="0 9 * * 1"), "cron:0 9 * * 1"),
])
def test_join_builds_what_loops_add_accepts(values, every):
    assert schedule.join(**values) == every
    assert schedule.parse_every(every)


def test_join_of_no_kind_is_empty_and_of_a_non_number_is_left_for_parse_every():
    assert schedule.join(None) == ""
    with pytest.raises(ValueError):
        schedule.parse_every(schedule.join("minutes", "abc"))


def test_split_round_trips_and_falls_back_to_cron():
    assert schedule.split("weekdays 09:00") == dict(kind="weekdays", number="", unit="minutes", at="09:00", cron="")
    assert schedule.split("hourly") == dict(kind="minutes", number="1", unit="hours", at="", cron="")
    assert schedule.split("45m")["number"] == "45" and schedule.split("45m")["unit"] == "minutes"
    assert schedule.split("120m")["number"] == "2" and schedule.split("120m")["unit"] == "hours"
    assert schedule.split("")["kind"] is None
    assert schedule.split("cron:0 9 * * 1")["cron"] == "0 9 * * 1"
    odd = schedule.split("every tuesday")
    assert odd["kind"] == "cron" and odd["cron"] == "every tuesday"
