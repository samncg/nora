from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from nora.timetable import DAYS, Timetable

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def tt() -> Timetable:
    loaded = Timetable.load(FIXTURES / "timetable.yaml")
    assert loaded is not None
    return loaded


def test_loads_the_example_timetable(tt):
    assert len(tt.slots) == 8
    assert tt.slots_on(0)[0].code == "CS 101 (T)"
    assert tt.slots_on(0)[0].room == "Room A"
    assert tt.slots_on(0)[0].tutor == "Dr. Example"


def test_weekend_has_no_classes(tt):
    assert tt.slots_on(DAYS.index("saturday")) == []
    assert tt.slots_on(DAYS.index("sunday")) == []


def test_slots_are_sorted_by_time(tt):
    tuesday = tt.slots_on(DAYS.index("tuesday"))
    starts = [slot.start.strftime("%H:%M") for slot in tuesday]
    assert starts == ["08:30", "10:00", "13:30"]


def test_current_class():
    tt = Timetable.load(FIXTURES / "timetable.yaml")
    # 2026-11-03 is a Tuesday, 09:00 falls inside PHYS 08:30-10:00
    slot = tt.current(datetime(2026, 11, 3, 9, 0))
    assert slot is not None
    assert slot.code == "PHYS"
    assert slot.room == "Lab 1"


def test_no_current_class_between_lessons():
    tt = Timetable.load(FIXTURES / "timetable.yaml")
    assert tt.current(datetime(2026, 11, 3, 11, 30)) is None


def test_describe_now_on_a_class_day():
    tt = Timetable.load(FIXTURES / "timetable.yaml")
    lines = tt.describe_now(datetime(2026, 11, 3, 9, 15))

    assert "he is in class right now: PHYS in Lab 1 with Dr. Demo, until 10:00." in lines
    assert (
        "his next class today is MATH (L) in Room C with Prof. Test "
        "at 10:00." in lines
    )


def test_describe_now_after_the_last_class():
    tt = Timetable.load(FIXTURES / "timetable.yaml")
    lines = tt.describe_now(datetime(2026, 11, 3, 17, 0))

    assert "he is done with classes for today." in lines
    assert any(line.startswith("his next class is wednesday") for line in lines)


def test_describe_now_on_sunday():
    tt = Timetable.load(FIXTURES / "timetable.yaml")
    lines = tt.describe_now(datetime(2026, 11, 1, 12, 0))

    assert "he has no classes today." in lines
    assert (
        "his next class is monday at 11:00, CS 101 (T) in Room A with Dr. Example."
        in lines
    )


def test_week_overview_has_five_days(tt):
    week = tt.describe_week()
    assert len(week) == 5
    assert week[0].startswith("monday: 11:00-12:00 CS 101 (T)")


def test_from_week_hides_early_classes():
    tt = Timetable.load(FIXTURES / "timetable.yaml")
    # CS 101 (T) only exists from week 3 onwards
    assert [s.code for s in tt.slots_on(0, week=1)] == ["ENG (L)"]
    assert [s.code for s in tt.slots_on(0, week=3)] == ["CS 101 (T)", "ENG (L)"]


def test_bad_entries_are_skipped():
    tt = Timetable.from_dict(
        {
            "classes": [
                {"day": "funday", "start": "10:00", "end": "11:00", "code": "X"},
                {"day": "monday", "start": "nope", "end": "11:00", "code": "Y"},
                {"day": "monday", "start": "10:00", "end": "11:00", "code": "OK"},
            ]
        }
    )
    assert [slot.code for slot in tt.slots] == ["OK"]
