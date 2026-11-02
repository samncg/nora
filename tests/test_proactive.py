from __future__ import annotations

import random
from datetime import datetime, timedelta

from nora.config import FollowUpConfig, ProactiveConfig
from nora.proactive import (
    decide,
    first_delay_seconds,
    in_quiet_hours,
    next_gap_seconds,
    quiet_end,
    should_follow_up,
)


def cfg(**overrides) -> ProactiveConfig:
    base = dict(
        enabled=True,
        user_id=123456789012345678,
        min_gap_minutes=150,
        max_gap_minutes=420,
        idle_minutes=45,
        quiet_hours=[0, 8],
        max_per_day=4,
        max_unanswered=3,
        chance=0.75,
    )
    base.update(overrides)
    return ProactiveConfig(**base)


NOW = datetime(2026, 11, 1, 15, 30)


def test_quiet_hours_normal_window():
    assert in_quiet_hours(NOW.replace(hour=3), [0, 8]) is True
    assert in_quiet_hours(NOW.replace(hour=8), [0, 8]) is False
    assert in_quiet_hours(NOW, [0, 8]) is False


def test_quiet_hours_wraps_midnight():
    assert in_quiet_hours(NOW.replace(hour=23), [23, 8]) is True
    assert in_quiet_hours(NOW.replace(hour=2), [23, 8]) is True
    assert in_quiet_hours(NOW.replace(hour=8), [23, 8]) is False
    assert in_quiet_hours(NOW, [23, 8]) is False


def test_quiet_hours_empty_window():
    assert in_quiet_hours(NOW.replace(hour=3), [0, 0]) is False


def test_quiet_end_same_day():
    end = quiet_end(NOW.replace(hour=3), [0, 8])
    assert end == NOW.replace(hour=8, minute=0, second=0, microsecond=0)


def test_quiet_end_after_midnight_wrap():
    end = quiet_end(NOW.replace(hour=23), [23, 8])
    assert end.day == 2
    assert end.hour == 8


def test_quiet_end_before_wrap_start():
    end = quiet_end(NOW.replace(hour=2), [23, 8])
    assert end.day == 1
    assert end.hour == 8


def test_next_gap_within_bounds():
    rng = random.Random(1)
    for _ in range(50):
        seconds = next_gap_seconds(cfg(), rng)
        assert 150 * 60 <= seconds <= 420 * 60


def test_first_delay_is_short():
    rng = random.Random(2)
    for _ in range(50):
        seconds = first_delay_seconds(cfg(), rng)
        assert 12 * 60 <= seconds <= 30 * 60


def test_decide_sends_when_everything_lines_up():
    decision = decide(
        NOW,
        last_activity=NOW - timedelta(hours=3),
        last_proactive=NOW - timedelta(hours=8),
        sent_today=1,
        unanswered=0,
        cfg=cfg(),
        roll=0.1,
    )
    assert decision.send is True


def test_decide_respects_each_guard():
    cases = [
        (cfg(enabled=False), dict(), "disabled"),
        (cfg(user_id=None), dict(), "no user id configured"),
        (cfg(), dict(now=NOW.replace(hour=2)), "quiet hours"),
        (cfg(), dict(sent_today=4), "daily limit reached"),
        (cfg(), dict(unanswered=3), "he is not answering"),
        (cfg(), dict(last_activity=NOW - timedelta(minutes=10)), "you talked recently"),
        (cfg(), dict(last_proactive=NOW - timedelta(minutes=30)), "too soon since the last one"),
        (cfg(), dict(roll=0.99), "coin flip"),
    ]
    for settings, overrides, reason in cases:
        now = overrides.pop("now", NOW)
        kwargs = dict(
            last_activity=NOW - timedelta(hours=3),
            last_proactive=NOW - timedelta(hours=8),
            sent_today=0,
            unanswered=0,
            roll=0.1,
        )
        kwargs.update(overrides)
        decision = decide(now, cfg=settings, **kwargs)
        assert decision.send is False, reason
        assert decision.reason == reason


def test_decide_fires_again_after_quiet_hours():
    decision = decide(
        NOW.replace(hour=9),
        last_activity=NOW.replace(hour=5),
        last_proactive=None,
        sent_today=0,
        unanswered=0,
        cfg=cfg(),
        roll=0.2,
    )
    assert decision.send is True


# -- following up on silence --------------------------------------------
def fup(**overrides) -> FollowUpConfig:
    base = dict(
        enabled=True, after_minutes=25, max_minutes=300, max_followups=1
    )
    base.update(overrides)
    return FollowUpConfig(**base)


def test_she_asks_where_he_went():
    decision = should_follow_up(
        NOW,
        idle_minutes=30,
        last_role="assistant",
        followups_sent=0,
        sent_today=0,
        cfg=cfg(),
    )
    assert decision.send is True


def test_no_follow_up_when_he_spoke_last():
    decision = should_follow_up(
        NOW,
        idle_minutes=30,
        last_role="user",
        followups_sent=0,
        sent_today=0,
        cfg=cfg(),
    )
    assert decision.send is False
    assert decision.reason == "he spoke last"


def test_follow_up_guards():
    cases = [
        (cfg(followup=fup(enabled=False)), dict(), "followups disabled"),
        (cfg(user_id=None), dict(), "no user id configured"),
        (cfg(), dict(idle_minutes=None), "no conversation yet"),
        (cfg(), dict(last_role=None), "no conversation yet"),
        (cfg(), dict(now=NOW.replace(hour=3)), "quiet hours"),
        (cfg(), dict(sent_today=4), "daily limit reached"),
        (cfg(followup=fup(max_followups=1)), dict(followups_sent=1), "already asked"),
        (cfg(), dict(minutes_since_followup=5), "asked recently"),
        (cfg(), dict(idle_minutes=5), "not gone long enough"),
        (cfg(), dict(idle_minutes=400), "gone too long"),
    ]
    for settings, overrides, reason in cases:
        kwargs = dict(
            idle_minutes=30,
            last_role="assistant",
            followups_sent=0,
            sent_today=0,
        )
        kwargs.update(overrides)
        now = kwargs.pop("now", NOW)
        decision = should_follow_up(now, cfg=settings, **kwargs)
        assert decision.send is False, reason
        assert decision.reason == reason


def test_follow_up_can_be_allowed_twice():
    decision = should_follow_up(
        NOW,
        idle_minutes=40,
        last_role="assistant",
        followups_sent=2,
        sent_today=0,
        cfg=cfg(),
        minutes_since_followup=30,
    )
    assert decision.send is False
    assert decision.reason == "already asked"


def test_no_follow_up_after_a_sign_off():
    for text in ("anyway goodnight", "gn", "im going to bed", "ok bye"):
        decision = should_follow_up(
            NOW,
            idle_minutes=60,
            last_role="assistant",
            followups_sent=0,
            sent_today=0,
            cfg=cfg(),
            last_user_text=text,
        )
        assert decision.send is False, text
        assert decision.reason == "he said goodnight"


def test_follow_up_still_happens_without_a_sign_off():
    decision = should_follow_up(
        NOW,
        idle_minutes=60,
        last_role="assistant",
        followups_sent=0,
        sent_today=0,
        cfg=cfg(),
        last_user_text="im back from class",
    )
    assert decision.send is True
