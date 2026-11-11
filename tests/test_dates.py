from __future__ import annotations

from datetime import date

from nora import dates

BASE = date(2026, 11, 3)


def test_iso_dates():
    assert dates.parse_date("exam on 2026-11-05", base=BASE) == date(2026, 11, 5)


def test_month_and_day():
    assert dates.parse_date("exam on november 5th", base=BASE) == date(2026, 11, 5)
    assert dates.parse_date("exam on Nov 5", base=BASE) == date(2026, 11, 5)
    assert dates.parse_date("exam on sept 12", base=BASE) == date(2026, 9, 12)


def test_day_before_month():
    assert dates.parse_date("exam on 5th of november", base=BASE) == date(2026, 11, 5)
    assert dates.parse_date("exam on 5 november", base=BASE) == date(2026, 11, 5)


def test_explicit_year_wins():
    assert dates.parse_date("exam on november 5, 2027", base=BASE) == date(2027, 11, 5)


def test_missing_year_rolls_forward_past_dates():
    # "january" said in november means the january that is coming up
    assert dates.parse_date("trip on january 4", base=BASE) == date(2027, 1, 4)
    # and "september" said in november means the one just gone
    assert dates.parse_date("trip on september 4", base=BASE) == date(2026, 9, 4)


def test_relative_days():
    assert dates.parse_date("exam tomorrow", base=BASE) == date(2026, 11, 4)
    assert dates.parse_date("exam tmr", base=BASE) == date(2026, 11, 4)
    assert dates.parse_date("exam the next day", base=BASE) == date(2026, 11, 4)
    assert dates.parse_date("exam tonight", base=BASE) == date(2026, 11, 3)


def test_no_date_is_none():
    assert dates.parse_date("he studies computer science", base=BASE) is None
    assert dates.parse_date("", base=BASE) is None
    assert dates.parse_date("he may be tired", base=BASE) is None


def test_impossible_dates_are_ignored():
    assert dates.parse_date("exam on february 31", base=BASE) is None
    assert dates.parse_date("exam on 2026-13-40", base=BASE) is None


def test_rewriting_a_relative_day():
    assert (
        dates.rewrite_relative("ncg has an exam tomorrow morning", date(2026, 11, 4))
        == "ncg has an exam on 2026-11-04 morning"
    )


def test_only_one_off_things_are_resolved():
    # a durable fact stays a fact
    assert dates.resolve("he studies computer science", base=BASE) is None
    assert dates.resolve("his birthday is on 3 march", base=BASE) is None
    # a dated exam becomes an event
    resolved = dates.resolve("he has an exam tomorrow", base=BASE)
    assert resolved is not None
    content, when = resolved
    assert when == date(2026, 11, 4)
    assert "2026-11-04" in content


def test_an_event_word_without_a_date_is_not_an_event():
    assert dates.resolve("he has exams coming up soon", base=BASE) is None


def test_relative_wording_is_perishable_on_its_own():
    # no event keyword, but "the next day" can never stay true
    resolved = dates.resolve("he has algebra at 2pm the next day", base=BASE)
    assert resolved is not None
    content, when = resolved
    assert when == date(2026, 11, 4)
    assert "2026-11-04" in content
