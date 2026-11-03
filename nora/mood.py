"""Persistent, slow-moving mood per person."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .memory import MemoryStore

# name: (prompt line, typing pace multiplier)
MOODS = {
    "warm": (
        "you are in a warm, soft mood. you want to hear about his day and make "
        "him feel looked after.",
        1.0,
    ),
    "playful": (
        "you are feeling playful and a little teasing. keep it light and cheeky.",
        0.95,
    ),
    "tired": (
        "you are tired and a little slow right now. short, soft replies; do not "
        "force energy.",
        1.2,
    ),
    "quiet": (
        "you are in a quiet mood. not upset, just low energy. let him carry some "
        "of the conversation.",
        1.12,
    ),
    "soft": (
        "you are feeling gentle and protective. he seems like he needs a softer you.",
        1.08,
    ),
}


@dataclass(frozen=True)
class Mood:
    name: str
    line: str
    pace: float


def current(
    store: MemoryStore,
    scope: str,
    now: datetime,
    *,
    sentiment: str = "",
    hold_minutes: int = 180,
) -> Mood:
    key = f"mood:{scope}"
    raw = store.kv_get(key)
    previous: str | None = None
    updated: datetime | None = None
    if raw:
        try:
            data = json.loads(raw)
            previous = str(data.get("name") or "") or None
            updated = datetime.fromisoformat(str(data["at"]))
        except (ValueError, KeyError, TypeError):
            previous = None
            updated = None

    # A genuinely sad message overrides a cheerful/general mood immediately.
    forced = sentiment == "sad" and previous != "soft"
    if (
        not forced
        and previous in MOODS
        and updated is not None
        and now - updated < timedelta(minutes=max(0, hold_minutes))
    ):
        return _mood(previous)

    name = choose(now, previous, sentiment)
    store.kv_set(
        key,
        json.dumps({"name": name, "at": now.isoformat(timespec="seconds")}),
    )
    return _mood(name)


def _mood(name: str) -> Mood:
    key = name if name in MOODS else "warm"
    line, pace = MOODS[key]
    return Mood(key, line, pace)


def choose(
    now: datetime, previous: str | None, sentiment: str
) -> str:
    if sentiment == "sad":
        return "soft"
    if sentiment == "affectionate":
        return "warm"
    if sentiment == "playful":
        return "playful"

    hour = now.hour
    if hour < 6 or hour >= 23:
        options = ["tired", "quiet"]
    elif hour < 10:
        options = ["soft", "warm"]
    elif hour < 17:
        options = ["warm", "playful"]
    elif hour < 22:
        options = ["playful", "warm"]
    else:
        options = ["tired", "quiet"]

    choices = [mood for mood in options if mood != previous] or options
    return random.choice(choices)


def sentiment_of(messages: list[dict[str, Any]]) -> str:
    text = " ".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    ).lower()
    if not text:
        return ""
    if any(
        word in text
        for word in (
            "sad",
            "cry",
            "crying",
            "stressed",
            "exhausted",
            "bad day",
            "lonely",
            "awful",
            "overwhelmed",
        )
    ):
        return "sad"
    if any(
        word in text
        for word in (
            "love you",
            "miss you",
            "ily",
            "thinking of you",
            "cuddle",
            "kiss",
            "sweet",
        )
    ):
        return "affectionate"
    if any(
        word in text
        for word in ("lol", "lmao", "haha", "bruh", "dead", "funny", "joking")
    ):
        return "playful"
    return ""
