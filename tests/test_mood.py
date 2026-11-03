from __future__ import annotations

from datetime import datetime, timedelta

from nora import mood
from nora.memory import MemoryStore


def test_sentiment_of():
    assert (
        mood.sentiment_of([{"role": "user", "content": "im so stressed"}])
        == "sad"
    )
    assert (
        mood.sentiment_of([{"role": "user", "content": "i miss you"}])
        == "affectionate"
    )
    assert (
        mood.sentiment_of([{"role": "user", "content": "lmao thats funny"}])
        == "playful"
    )
    assert mood.sentiment_of([{"role": "assistant", "content": "im sad"}]) == ""


def test_mood_is_held_then_can_shift(tmp_path):
    store = MemoryStore(tmp_path / "mood.db")
    now = datetime(2026, 11, 3, 14, 0)

    first = mood.current(store, "u:1", now, sentiment="sad", hold_minutes=180)
    assert first.name == "soft"

    held = mood.current(
        store,
        "u:1",
        now + timedelta(minutes=30),
        sentiment="playful",
        hold_minutes=180,
    )
    assert held.name == "soft"

    shifted = mood.current(
        store,
        "u:1",
        now + timedelta(minutes=200),
        sentiment="playful",
        hold_minutes=180,
    )
    assert shifted.name == "playful"
    store.close()


def test_sad_message_overrides_a_held_mood(tmp_path):
    store = MemoryStore(tmp_path / "mood2.db")
    now = datetime(2026, 11, 3, 14, 0)

    mood.current(store, "u:1", now, sentiment="playful", hold_minutes=180)
    shifted = mood.current(
        store,
        "u:1",
        now + timedelta(minutes=5),
        sentiment="sad",
        hold_minutes=180,
    )
    assert shifted.name == "soft"
    store.close()
