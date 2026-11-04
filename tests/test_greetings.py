from __future__ import annotations

from datetime import datetime
from typing import Any

from nora import greetings
from nora.config import GreetingsConfig
from nora.memory import MemoryStore


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 4, hour, minute)


def cfg(**overrides: Any) -> GreetingsConfig:
    base: dict[str, Any] = dict(
        enabled=True,
        morning_hour=8,
        morning_window_hours=3,
        night_hour=23,
        night_window_hours=2,
        chance=1.0,
        skip_if_chatted_minutes=30,
    )
    base.update(overrides)
    return GreetingsConfig(**base)


def test_window_covers_its_own_hours_only():
    assert greetings.in_window(at(8), 8, 3) is True
    assert greetings.in_window(at(10, 59), 8, 3) is True
    assert greetings.in_window(at(11), 8, 3) is False
    assert greetings.in_window(at(7, 59), 8, 3) is False


def test_window_wraps_past_midnight():
    assert greetings.in_window(at(23), 23, 2) is True
    assert greetings.in_window(at(0, 30), 23, 2) is True
    assert greetings.in_window(at(1), 23, 2) is False
    assert greetings.in_window(at(12), 23, 2) is False


def test_zero_window_is_off():
    assert greetings.in_window(at(8), 8, 0) is False


def test_a_morning_greeting_is_owed_in_the_morning():
    assert greetings.owed(at(9), cfg(), {}) == greetings.MORNING


def test_no_greeting_outside_the_windows():
    assert greetings.owed(at(15), cfg(), {}) is None


def test_once_per_day():
    sent = {greetings.MORNING: "2026-11-04"}
    assert greetings.owed(at(9), cfg(), sent) is None
    # tomorrow it is owed again
    assert greetings.owed(datetime(2026, 11, 5, 9), cfg(), sent) == greetings.MORNING


def test_night_wins_near_midnight():
    assert greetings.owed(at(23, 30), cfg(), {}) == greetings.NIGHT


def test_night_is_not_sent_twice():
    sent = {greetings.NIGHT: "2026-11-04"}
    assert greetings.owed(at(23, 45), cfg(), sent) is None


def test_disabled_sends_nothing():
    assert greetings.due(at(9), cfg(enabled=False), {}, roll=0.0) is None


def test_no_greeting_right_after_a_conversation():
    assert (
        greetings.due(at(9), cfg(), {}, chatted_minutes=5.0, roll=0.0) is None
    )
    assert (
        greetings.due(at(9), cfg(), {}, chatted_minutes=90.0, roll=0.0)
        == greetings.MORNING
    )


def test_the_coin_flip_can_skip_it():
    assert greetings.due(at(9), cfg(chance=0.2), {}, roll=0.9) is None
    assert greetings.due(at(9), cfg(chance=0.9), {}, roll=0.1) == greetings.MORNING


def test_marking_is_persisted_per_person(tmp_path):
    will = MemoryStore(tmp_path / "will.db")
    someone_else = MemoryStore(tmp_path / "else.db")

    greetings.mark(will, greetings.MORNING, at(9))

    assert greetings.load_sent(will) == {greetings.MORNING: "2026-11-04"}
    assert greetings.load_sent(someone_else) == {}


def test_corrupt_log_is_ignored(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    store.kv_set(greetings.KV_KEY, "[]")
    assert greetings.load_sent(store) == {}


def test_fallbacks_sound_like_her():
    assert greetings.fallback(greetings.MORNING).startswith("morning")
    assert greetings.fallback(greetings.NIGHT).startswith("night")


# -- learning his actual sleep schedule -------------------------------------
def test_it_notices_when_he_says_goodnight(tmp_path):
    store = MemoryStore(tmp_path / "n.db")

    assert greetings.note_user_greeting(store, "ok im going to bed, night", at(23, 40)) == (
        greetings.NIGHT,
        1,
    )
    assert greetings.note_user_greeting(store, "gn", at(0, 10)) == (
        greetings.NIGHT,
        2,
    )
    assert greetings.note_user_greeting(store, "good morning", at(8, 5)) == (
        greetings.MORNING,
        1,
    )


def test_normal_chatter_is_not_a_greeting(tmp_path):
    store = MemoryStore(tmp_path / "n.db")

    assert greetings.note_user_greeting(store, "im so tired lol", at(23, 0)) is None
    assert greetings.note_user_greeting(store, "what are you doing", at(9, 0)) is None
    assert greetings.note_user_greeting(store, "i hate mornings", at(9, 0)) is None


def test_the_profile_is_per_person(tmp_path):
    will = MemoryStore(tmp_path / "will.db")
    someone_else = MemoryStore(tmp_path / "else.db")

    greetings.note_user_greeting(will, "goodnight", at(23, 30))

    assert greetings.sample_hours(greetings.load_profile(will), greetings.NIGHT)
    assert greetings.sample_hours(greetings.load_profile(someone_else), greetings.NIGHT) == []


def test_circular_mean_wraps_past_midnight():
    # half the samples before midnight, half after
    assert greetings.circular_mean([23.5, 0.5]) == 0.0
    assert round(greetings.circular_mean([7.5, 8.5]), 2) == 8.0


def test_the_morning_window_follows_his_wake_up_time():
    profile = {greetings.MORNING: ["2026-11-01T07:40", "2026-11-02T07:50", "2026-11-03T07:30"]}
    start, span = greetings.window_for(greetings.MORNING, cfg(), profile)

    # mean is about 07:40, and she gets there 15 minutes early
    assert greetings.clock(start) == "07:25"
    assert span == 3


def test_the_night_window_follows_his_bedtime():
    profile = {greetings.NIGHT: ["2026-11-01T01:10", "2026-11-02T01:20", "2026-11-03T01:00"]}
    start, _ = greetings.window_for(greetings.NIGHT, cfg(), profile)

    # about 01:10, half an hour earlier
    assert greetings.clock(start) == "00:40"


def test_it_needs_a_few_samples_before_it_moves():
    profile = {greetings.MORNING: ["2026-11-01T07:40", "2026-11-02T07:40"]}
    start, _ = greetings.window_for(greetings.MORNING, cfg(min_samples=3), profile)
    assert start == 8.0  # still the configured hour


def test_adapting_can_be_turned_off():
    profile = {greetings.MORNING: ["2026-11-01T07:40"] * 10}
    start, _ = greetings.window_for(
        greetings.MORNING, cfg(adapt_to_sleep=False), profile
    )
    assert start == 8.0


def test_a_learned_window_fires_at_his_time():
    profile = {
        greetings.MORNING: [
            "2026-11-01T07:40",
            "2026-11-02T07:40",
            "2026-11-03T07:40",
        ]
    }
    # 07:30 is outside the configured 08:00 window, but inside the learned one
    assert greetings.owed(at(7, 30), cfg(), {}, profile) == greetings.MORNING
    assert greetings.owed(at(7, 30), cfg(), {}) is None
    # and 07:00 is still too early
    assert greetings.owed(at(7, 0), cfg(), {}, profile) is None


def test_describe_windows_says_where_it_got_the_hours():
    profile = {greetings.MORNING: ["2026-11-01T07:40"] * 4}
    line = greetings.describe_windows(cfg(), profile)
    assert "morning 07:25+3h (learned, 4 samples)" in line
    assert "night 23:00+2h (config, 0 samples)" in line


def test_clock_formatting():
    assert greetings.clock(8) == "08:00"
    assert greetings.clock(23.5) == "23:30"
    assert greetings.clock(0.25) == "00:15"


# -- starting from his message history, before any greetings -----------------
def _insert_at(store: MemoryStore, scope: str, hour: int) -> None:
    store._db.execute(
        "INSERT INTO messages (scope, role, content, created_at)"
        " VALUES (?, 'user', ?, ?)",
        (scope, "hi", f"2026-11-01T{hour:02d}:00:00"),
    )
    store._db.commit()


def test_the_seed_comes_from_when_he_is_usually_asleep():
    assert greetings.seed_from_sleep_window((0, 8)) == {
        greetings.MORNING: 8.0,
        greetings.NIGHT: 23.5,
    }
    assert greetings.seed_from_sleep_window((1, 9)) == {
        greetings.MORNING: 9.0,
        greetings.NIGHT: 0.5,
    }


def test_bad_sleep_windows_do_not_seed_anything():
    assert greetings.seed_from_sleep_window(None) is None
    assert greetings.seed_from_sleep_window((5, 5)) is None
    assert greetings.seed_from_sleep_window(()) is None


def test_his_history_seeds_the_windows(tmp_path):
    store = MemoryStore(tmp_path / "n.db")
    # he is up from 8am to midnight, and quiet from 1 to 7
    for hour in list(range(8, 24)) + [0]:
        for _ in range(3):
            _insert_at(store, "u:1", hour)

    seed = greetings.seed_from_sleep_window(store.quiet_window("u:1", hours=8))

    assert seed == {greetings.MORNING: 8.0, greetings.NIGHT: 23.5}


def test_the_seed_is_used_until_she_has_heard_him_say_it():
    seed = {greetings.MORNING: 6.0, greetings.NIGHT: 22.5}

    start, span = greetings.window_for(greetings.MORNING, cfg(), None, seed)
    assert start == 6.0
    assert span == 3

    assert greetings.owed(at(6, 10), cfg(), {}, None, seed) == greetings.MORNING
    # without the seed the configured 08:00 window still applies
    assert greetings.owed(at(6, 10), cfg(), {}) is None


def test_what_he_actually_says_beats_the_history():
    seed = {greetings.MORNING: 6.0}
    profile = {greetings.MORNING: ["2026-11-01T07:40"] * 3}

    start, _ = greetings.window_for(greetings.MORNING, cfg(), profile, seed)

    assert greetings.clock(start) == "07:25"


def test_adapting_off_ignores_the_history_too():
    seed = {greetings.MORNING: 6.0}
    start, _ = greetings.window_for(
        greetings.MORNING, cfg(adapt_to_sleep=False), None, seed
    )
    assert start == 8.0


def test_describe_windows_calls_the_seed_history():
    seed = {greetings.MORNING: 6.0, greetings.NIGHT: 22.5}
    line = greetings.describe_windows(cfg(), None, seed)

    assert "morning 06:00+3h (history, 0 samples)" in line
    assert "night 22:30+2h (history, 0 samples)" in line
