from __future__ import annotations

from datetime import datetime

from nora import config as config_module
from nora.config import PresenceConfig
from nora.presence import compute, in_busy_window, is_asleep, just_woke


def cfg(**overrides) -> PresenceConfig:
    base = dict(
        enabled=True,
        online_activity="on my phone",
        online_emoji="📱",
        sleep_hours=[1, 8],
        idle_activity="sleeping",
        idle_emoji="💤",
        busy_hours=[],
        busy_activity="busy",
        busy_emoji="🎧",
        refresh_minutes=5,
    )
    base.update(overrides)
    return PresenceConfig(**base)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 2, hour, minute)


def test_awake_means_online_on_her_phone():
    state = compute(at(15), cfg())
    assert state.status == "online"
    assert state.activity == "on my phone"
    assert state.emoji == "📱"


def test_she_is_idle_while_asleep():
    state = compute(at(3), cfg())
    assert state.status == "idle"
    assert state.activity == "sleeping"
    assert state.emoji == "💤"


def test_sleep_window_wraps_midnight():
    assert is_asleep(at(23), cfg(sleep_hours=[23, 6])) is True
    assert is_asleep(at(2), cfg(sleep_hours=[23, 6])) is True
    assert is_asleep(at(12), cfg(sleep_hours=[23, 6])) is False


def test_sleep_window_edges():
    # [1, 8) means 01:00 up to but not including 08:00
    assert is_asleep(at(1), cfg()) is True
    assert is_asleep(at(7, 59), cfg()) is True
    assert is_asleep(at(8), cfg()) is False


def test_busy_window_is_dnd():
    state = compute(at(10), cfg(busy_hours=[[9, 12]]))
    assert state.status == "dnd"
    assert state.activity == "busy"
    assert in_busy_window(at(11), cfg(busy_hours=[[9, 12]])) is True


def test_sleep_beats_busy():
    state = compute(at(3), cfg(sleep_hours=[1, 8], busy_hours=[[1, 8]]))
    assert state.status == "idle"


def test_empty_sleep_window_means_always_up():
    assert is_asleep(at(3), cfg(sleep_hours=[])) is False
    assert compute(at(3), cfg(sleep_hours=[])).status == "online"


def test_disabled_is_plain_online_without_activity():
    state = compute(at(3), cfg(enabled=False))
    assert state.status == "online"
    assert state.activity == ""


def test_just_woke_window():
    from datetime import datetime

    assert just_woke(datetime(2026, 11, 3, 8, 10), [1, 8], 45) is True
    assert just_woke(datetime(2026, 11, 3, 9, 0), [1, 8], 45) is False
    assert just_woke(datetime(2026, 11, 3, 7, 30), [1, 8], 45) is False
    assert just_woke(datetime(2026, 11, 3, 7, 10), [23, 7], 45) is True
    assert just_woke(datetime(2026, 11, 3, 8, 10), [], 45) is False
    assert just_woke(datetime(2026, 11, 3, 8, 10), [1, 8], 0) is False


def test_real_config_is_parsed():
    settings = config_module.load()
    presence = settings.presence
    assert presence.enabled is True
    assert presence.sleep_hours == [1, 8]
    assert presence.online_emoji == "📱"
    assert presence.online_activity == "on my phone"
    assert all(isinstance(w, list) and len(w) == 2 for w in presence.busy_hours)
