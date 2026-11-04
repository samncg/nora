from __future__ import annotations

import random
from datetime import datetime, timedelta

from nora import reminders
from nora.config import RemindersConfig
from nora.memory import MemoryStore
from nora.reminders import Reminder

# a wednesday afternoon, so "next monday" is unambiguous
NOW = datetime(2026, 11, 4, 15, 0)


def at(
    text: str,
    now: datetime = NOW,
    cfg: RemindersConfig | None = None,
    rng: random.Random | None = None,
):
    item = reminders.extract(text, now, cfg, rng)
    return item.at if item else None


def test_exam_tomorrow_at_2pm():
    item = reminders.extract("i have an exam tmr at 2pm", NOW, rng=random.Random(0))
    assert item is not None
    assert item.title == "exam"
    assert item.at == datetime(2026, 11, 5, 14, 0)
    assert 10 <= item.lead_minutes <= 50
    assert item.notify == item.at - timedelta(minutes=item.lead_minutes)
    # the reminder always lands before the event itself
    assert item.notify < item.at


def test_the_word_before_the_event_stays_in_the_title():
    item = reminders.extract("i have a chemistry exam tomorrow at 9:30am", NOW)
    assert item is not None
    assert item.title == "chemistry exam"
    assert item.at == datetime(2026, 11, 5, 9, 30)


def test_today_keeps_todays_date():
    assert at("meeting today at 4pm") == datetime(2026, 11, 4, 16, 0)


def test_a_time_in_the_past_rolls_to_tomorrow():
    # 2pm was two hours ago, so she assumes he means tomorrow
    assert at("interview at 2pm") == datetime(2026, 11, 5, 14, 0)


def test_weekday_names_use_the_next_one():
    assert at("lecture on monday at 9am") == datetime(2026, 11, 9, 9, 0)


def test_a_weekday_that_already_passed_rolls_a_full_week():
    # wednesday 9am has gone, so "wednesday at 9am" means next week
    assert at("class on wednesday at 9am") == datetime(2026, 11, 11, 9, 0)


def test_no_time_uses_the_default_hour():
    cfg = RemindersConfig(default_hour=7)
    assert at("exam tomorrow", cfg=cfg) == datetime(2026, 11, 5, 7, 0)


def test_relative_hours():
    assert at("have a meeting in 2 hours") == datetime(2026, 11, 4, 17, 0)
    assert at("call in 30 mins") is None  # no event word
    assert at("presentation in 45 mins") == datetime(2026, 11, 4, 15, 45)


def test_named_times_of_day():
    assert at("deadline tonight") == datetime(2026, 11, 4, 20, 0)
    assert at("exam tomorrow morning") == datetime(2026, 11, 5, 9, 0)


def test_bare_hour_after_at_is_afternoon_or_evening():
    # "at 5" reads as 5pm, and it is still ahead of him
    assert at("dentist at 5") == datetime(2026, 11, 4, 17, 0)
    # "at 9" reads as 9am, which has already gone today
    assert at("exam tmr at 9") == datetime(2026, 11, 5, 9, 0)


def test_past_events_are_ignored():
    assert at("i had an exam today") is None
    assert at("i passed my driving test") is None
    assert at("that test was brutal") is None
    assert at("i failed the quiz lol") is None
    assert at("exam yesterday was rough") is None


def test_negations_are_ignored():
    assert at("i dont have an exam tmr") is None


def test_ordinary_chat_is_not_an_event():
    assert at("you free tomorrow?") is None
    assert at("im so tired lol") is None
    assert at("what are you doing") is None


def test_empty_text_is_safe():
    assert reminders.extract("", NOW) is None
    assert reminders.extract("   ", NOW) is None


def test_disabled_config_stores_nothing(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    cfg = RemindersConfig(enabled=False)
    assert reminders.remember(store, "exam tmr at 2pm", NOW, cfg) is None
    assert reminders.load(store) == []


def test_remember_round_trips_through_the_store(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    cfg = RemindersConfig()
    item = reminders.remember(store, "i have an exam tmr at 2pm", NOW, cfg)

    assert item is not None
    loaded = reminders.load(store)
    assert len(loaded) == 1
    assert loaded[0].title == "exam"
    assert loaded[0].at == datetime(2026, 11, 5, 14, 0)


def test_restating_an_event_replaces_it(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    cfg = RemindersConfig()
    reminders.remember(store, "i have an exam tmr at 2pm", NOW, cfg)
    reminders.remember(store, "exam tmrw at 2pm", NOW, cfg)
    assert len(reminders.load(store)) == 1


def test_two_different_exams_on_different_days_both_stay(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    cfg = RemindersConfig()
    reminders.remember(store, "exam tmr at 2pm", NOW, cfg)
    reminders.remember(store, "exam on monday at 2pm", NOW, cfg)
    assert len(reminders.load(store)) == 2


def test_corrupt_storage_is_not_fatal(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    store.kv_set(reminders.KV_KEY, "{not json")
    assert reminders.load(store) == []


def test_the_lead_moves_around_between_events():
    cfg = RemindersConfig(lead_minutes=30, lead_jitter_minutes=20)
    rng = random.Random(7)
    leads = {reminders.choose_lead(cfg, rng) for _ in range(40)}

    assert leads <= set(range(10, 51))
    assert min(leads) >= 10
    assert max(leads) <= 50
    # it should not be one fixed number every single time
    assert len(leads) > 5


def test_zero_jitter_is_the_plain_lead():
    cfg = RemindersConfig(lead_minutes=30, lead_jitter_minutes=0)
    assert reminders.choose_lead(cfg, random.Random(1)) == 30


def test_the_lead_never_goes_past_the_event():
    cfg = RemindersConfig(lead_minutes=5, lead_jitter_minutes=40, min_lead_minutes=5)
    rng = random.Random(3)
    assert all(reminders.choose_lead(cfg, rng) >= 5 for _ in range(20))


def test_due_uses_the_picked_notify_time():
    item = Reminder(
        title="exam",
        when=datetime(2026, 11, 4, 16, 0).isoformat(timespec="minutes"),
        lead_minutes=45,
        notify_at=datetime(2026, 11, 4, 15, 15).isoformat(timespec="minutes"),
    )
    assert reminders.due([item], datetime(2026, 11, 4, 15, 14)) == []
    assert reminders.due([item], datetime(2026, 11, 4, 15, 15)) == [item]
    # the stored lead is not what decides it
    assert reminders.due([item], datetime(2026, 11, 4, 15, 20)) == [item]


def test_old_reminders_without_a_notify_time_still_work():
    item = Reminder(
        title="exam",
        when=datetime(2026, 11, 4, 14, 0).isoformat(timespec="minutes"),
        lead_minutes=30,
    )
    assert item.notify == datetime(2026, 11, 4, 13, 30)


def test_due_opens_at_the_lead_time():
    item = Reminder(
        title="exam",
        when=datetime(2026, 11, 4, 14, 0).isoformat(timespec="minutes"),
        lead_minutes=30,
    )
    assert reminders.due([item], datetime(2026, 11, 4, 13, 29)) == []
    assert reminders.due([item], datetime(2026, 11, 4, 13, 30)) == [item]
    assert reminders.due([item], datetime(2026, 11, 4, 14, 0)) == [item]
    # once it has started it is too late for a heads up
    assert reminders.due([item], datetime(2026, 11, 4, 14, 1)) == []


def test_a_notified_reminder_does_not_fire_again():
    item = Reminder(
        title="exam",
        when=datetime(2026, 11, 4, 14, 0).isoformat(timespec="minutes"),
        lead_minutes=30,
        notified_at="2026-11-04T13:30:00",
    )
    assert reminders.due([item], datetime(2026, 11, 4, 13, 31)) == []


def test_prune_drops_old_events_and_keeps_upcoming_ones():
    past = Reminder(title="old", when=datetime(2026, 11, 1, 9, 0).isoformat())
    soon = Reminder(title="exam", when=datetime(2026, 11, 5, 9, 0).isoformat())
    kept = reminders.prune([past, soon], NOW)
    assert [item.title for item in kept] == ["exam"]


def test_wording_reads_like_her():
    assert reminders.format_when(datetime(2026, 11, 4, 14, 0), NOW) == "at 2pm today"
    assert reminders.format_when(datetime(2026, 11, 5, 14, 0), NOW) == "at 2pm tomorrow"
    assert reminders.format_when(datetime(2026, 11, 9, 9, 0), NOW) == "on monday at 9am"
    assert (
        reminders.format_when(datetime(2026, 11, 20, 9, 30), NOW)
        == "on 20 november at 9:30am"
    )


def test_reminder_text_is_the_promise_she_made():
    item = Reminder(
        title="exam", when=datetime(2026, 11, 5, 14, 0).isoformat(timespec="minutes")
    )
    text = reminders.reminder_text([item], NOW)
    assert text == "hey, your exam is at 2pm tomorrow. dont forget. good luck"


def test_upcoming_is_short_and_skips_what_already_happened():
    items = [
        Reminder(title="exam", when=datetime(2026, 11, 5, 14, 0).isoformat()),
        Reminder(title="old", when=datetime(2026, 11, 3, 9, 0).isoformat()),
        Reminder(title="far", when=datetime(2026, 12, 1, 9, 0).isoformat()),
    ]
    lines = reminders.upcoming(items, NOW, days=2)
    assert lines == ["exam at 2pm tomorrow"]


def test_upcoming_ignores_notified_events():
    items = [
        Reminder(
            title="exam",
            when=datetime(2026, 11, 5, 14, 0).isoformat(),
            notified_at="2026-11-04T13:30:00",
        )
    ]
    assert reminders.upcoming(items, NOW, days=2) == []
