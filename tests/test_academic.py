from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from nora.academic import AcademicCalendar

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def cal() -> AcademicCalendar:
    loaded = AcademicCalendar.load(FIXTURES / "calendar.yaml")
    assert loaded is not None
    return loaded


def test_study_week(cal):
    status = cal.status(date(2026, 10, 26))
    assert status.kind == "study"
    assert status.until == date(2026, 11, 8)


def test_exam_week(cal):
    assert cal.status(date(2026, 11, 10)).kind == "exam"


def test_semester_break(cal):
    status = cal.status(date(2026, 11, 20))
    assert status.kind == "break"
    assert status.until == date(2026, 11, 29)


def test_public_holiday_wins_over_study_week(cal):
    # The example holiday falls inside study week, so it takes priority.
    status = cal.status(date(2026, 11, 8))
    assert status.kind == "holiday"
    assert status.label == "example holiday"


def test_normal_teaching_day(cal):
    status = cal.status(date(2026, 9, 7))
    assert status.kind == "teaching"
    assert status.classes_run is True


def test_classes_do_not_run_on_a_break(cal):
    assert cal.status(date(2026, 11, 20)).classes_run is False


def test_describe_break(cal):
    lines = cal.describe(date(2026, 11, 20))
    assert "he is on semester break until 29 november. no classes." in lines


def test_describe_exam(cal):
    assert "it is his final exam week until 14 november." in cal.describe(date(2026, 11, 10))


def test_describe_holiday(cal):
    assert "today is example holiday, so no classes." in cal.describe(date(2026, 8, 31))


def test_upcoming_events(cal):
    lines = cal.describe(date(2026, 10, 20))
    assert "study week is in 5 days (25 october)." in lines


def test_describe_quiet_day_is_empty(cal):
    # nothing within the 10 day lookahead from here either
    assert cal.describe(date(2026, 9, 20)) == []


def test_bad_entries_are_skipped():
    cal = AcademicCalendar.from_dict(
        {
            "events": [
                {"kind": "party", "start": "2026-01-01"},
                {"kind": "holiday", "start": "not a date"},
                {"kind": "holiday", "date": "2026-03-03", "label": "ok day"},
            ]
        }
    )
    assert len(cal.events) == 1
    assert cal.status(date(2026, 3, 3)).kind == "holiday"
