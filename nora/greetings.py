"""Good morning and goodnight, once per day each.

The two windows start from the config values and then follow him: whenever he
says "good morning" or "goodnight" to her, the hour is remembered, and the
window moves to match his actual sleep schedule. Pure logic, so it is all
testable without a clock or a discord connection.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

from .config import GreetingsConfig
from .memory import MemoryStore

KV_KEY = "greetings"
PROFILE_KEY = "greeting_profile"

MORNING = "morning"
NIGHT = "night"

MAX_SAMPLES = 30
# She gets there a little first: texts good morning before he does, and says
# goodnight before he is actually asleep.
MORNING_LEAD_HOURS = 0.25
NIGHT_LEAD_HOURS = 0.5

FALLBACKS = {
    MORNING: "morning. sleep ok?",
    NIGHT: "night. dont stay up too late",
}

_NIGHT_RE = re.compile(
    r"\b("
    r"good\s*night|goodnight|night\s*night|nighty night|gn|"
    r"going to bed|going to sleep|gonna sleep|off to bed|heading to bed|bedtime"
    r")\b"
)
_MORNING_RE = re.compile(r"\b(good\s*morning|gm)\b")
_MORNING_START_RE = re.compile(r"^\s*morning\b")


# -- time windows -----------------------------------------------------
def _minutes(now) -> int:
    return now.hour * 60 + now.minute


def in_window(now, start_hour: float, span_hours: float) -> bool:
    """[start, start + span), wrapping past midnight. Floats are fine."""
    span = max(0.0, float(span_hours))
    if span <= 0:
        return False
    begin = round((float(start_hour) % 24) * 60)
    end = begin + round(span * 60)
    current = _minutes(now)
    if end <= 24 * 60:
        return begin <= current < end
    return current >= begin or current < end - 24 * 60


# -- what he actually does --------------------------------------------
def _kind_of(text: str) -> str | None:
    body = " ".join((text or "").replace("\u2019", "'").lower().split())
    if not body:
        return None
    if _NIGHT_RE.search(body):
        return NIGHT
    if _MORNING_RE.search(body) or _MORNING_START_RE.search(body):
        return MORNING
    return None


def load_profile(store: MemoryStore) -> dict[str, list[str]]:
    """{kind: [ISO minute stamps of when he said it]}"""
    raw = store.kv_get(PROFILE_KEY)
    out: dict[str, list[str]] = {MORNING: [], NIGHT: []}
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for kind in (MORNING, NIGHT):
        values = data.get(kind)
        if isinstance(values, list):
            out[kind] = [str(v) for v in values][-MAX_SAMPLES:]
    return out


def save_profile(store: MemoryStore, profile: dict[str, list[str]]) -> None:
    payload = {
        kind: list(profile.get(kind, []))[-MAX_SAMPLES:]
        for kind in (MORNING, NIGHT)
    }
    store.kv_set(PROFILE_KEY, json.dumps(payload, ensure_ascii=False))


def note_user_greeting(
    store: MemoryStore, text: str, now: datetime
) -> tuple[str, int] | None:
    """Remember when he said good morning / goodnight. Returns (kind, samples)."""
    kind = _kind_of(text)
    if kind is None:
        return None
    profile = load_profile(store)
    profile[kind] = profile[kind] + [now.replace(second=0, microsecond=0).isoformat(
        timespec="minutes"
    )]
    save_profile(store, profile)
    return kind, len(profile[kind])


def sample_hours(profile: dict[str, list[str]] | None, kind: str) -> list[float]:
    """The hour-of-day of each remembered greeting, in hours, minutes included."""
    hours: list[float] = []
    for stamp in (profile or {}).get(kind, []) or []:
        try:
            moment = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        hours.append(moment.hour + moment.minute / 60.0)
    return hours


def circular_mean(hours: list[float]) -> float:
    """Average a set of clock hours, so 23:30 and 00:30 average to midnight."""
    if not hours:
        return 0.0
    angles = [h / 24.0 * 2.0 * math.pi for h in hours]
    sin = sum(math.sin(a) for a in angles) / len(angles)
    cos = sum(math.cos(a) for a in angles) / len(angles)
    if abs(sin) < 1e-9 and abs(cos) < 1e-9:
        return hours[0]
    mean = (math.atan2(sin, cos) % (2.0 * math.pi)) / (2.0 * math.pi) * 24.0
    return mean % 24.0


def window_for(
    kind: str,
    cfg: GreetingsConfig,
    profile: dict[str, list[str]] | None = None,
    seed: dict[str, float] | None = None,
) -> tuple[float, float]:
    """(start hour, span hours) for a greeting.

    Priority: what he actually says (learned), then what his message history
    says about his sleep (seed), then the fixed hours in the config.
    """
    default_start = cfg.morning_hour if kind == MORNING else cfg.night_hour
    span = (
        cfg.morning_window_hours if kind == MORNING else cfg.night_window_hours
    )
    if not cfg.adapt_to_sleep:
        return float(default_start), span
    hours = sample_hours(profile, kind)
    if len(hours) >= max(1, int(cfg.min_samples)):
        offset = MORNING_LEAD_HOURS if kind == MORNING else NIGHT_LEAD_HOURS
        return (circular_mean(hours) - offset) % 24.0, span
    if seed and kind in seed:
        return float(seed[kind]) % 24.0, span
    return float(default_start), span


def seed_from_sleep_window(
    sleep_window: tuple[int, int] | None,
) -> dict[str, float] | None:
    """Turn "he is usually asleep between a and b" into starting hours.

    He wakes up when the quiet stretch ends, so the morning greeting lands
    there; he goes quiet when it starts, so goodnight comes half an hour first.
    """
    if not sleep_window or len(sleep_window) < 2:
        return None
    try:
        start = int(sleep_window[0]) % 24
        end = int(sleep_window[1]) % 24
    except (TypeError, ValueError):
        return None
    if start == end:
        return None
    return {
        MORNING: float(end),
        NIGHT: (start - NIGHT_LEAD_HOURS) % 24.0,
    }


def _source(
    kind: str,
    cfg: GreetingsConfig,
    profile: dict[str, list[str]] | None,
    seed: dict[str, float] | None,
) -> str:
    if not cfg.adapt_to_sleep:
        return "config"
    if len(sample_hours(profile, kind)) >= max(1, int(cfg.min_samples)):
        return "learned"
    if seed and kind in seed:
        return "history"
    return "config"


def clock(hour: float) -> str:
    """8.25 -> 08:15"""
    total = round((float(hour) % 24) * 60) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def describe_windows(
    cfg: GreetingsConfig,
    profile: dict[str, list[str]] | None = None,
    seed: dict[str, float] | None = None,
) -> str:
    """For the log: where the two windows sit and why."""
    parts = []
    for kind in (MORNING, NIGHT):
        start, span = window_for(kind, cfg, profile, seed)
        samples = len(sample_hours(profile, kind))
        parts.append(
            f"{kind} {clock(start)}+{span:g}h "
            f"({_source(kind, cfg, profile, seed)}, {samples} samples)"
        )
    return ", ".join(parts)


# -- what she has already sent ----------------------------------------
def load_sent(store: MemoryStore) -> dict[str, str]:
    raw = store.kv_get(KV_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_sent(store: MemoryStore, sent: dict[str, str]) -> None:
    store.kv_set(KV_KEY, json.dumps(sent, ensure_ascii=False))


def mark(store: MemoryStore, kind: str, now) -> None:
    sent = load_sent(store)
    sent[kind] = now.date().isoformat()
    save_sent(store, sent)


def owed(
    now,
    cfg: GreetingsConfig,
    sent: dict[str, str],
    profile: dict[str, list[str]] | None = None,
    seed: dict[str, float] | None = None,
) -> str | None:
    """Which greeting is due right now, if any. Night wins near midnight."""
    if not cfg.enabled:
        return None
    today = now.date().isoformat()
    night_start, night_span = window_for(NIGHT, cfg, profile, seed)
    if sent.get(NIGHT) != today and in_window(now, night_start, night_span):
        return NIGHT
    morning_start, morning_span = window_for(MORNING, cfg, profile, seed)
    if sent.get(MORNING) != today and in_window(now, morning_start, morning_span):
        return MORNING
    return None


def due(
    now,
    cfg: GreetingsConfig,
    sent: dict[str, str],
    *,
    profile: dict[str, list[str]] | None = None,
    seed: dict[str, float] | None = None,
    chatted_minutes: float | None = None,
    roll: float = 0.0,
) -> str | None:
    """The greeting to send now, or None.

    `chatted_minutes` is how long since the last text in either direction. She
    does not say good morning to someone she was just talking to.
    """
    kind = owed(now, cfg, sent, profile, seed)
    if kind is None:
        return None
    if (
        chatted_minutes is not None
        and chatted_minutes < float(cfg.skip_if_chatted_minutes)
    ):
        return None
    if roll > max(0.0, min(1.0, float(cfg.chance))):
        return None
    return kind


def fallback(kind: str) -> str:
    return FALLBACKS.get(kind, FALLBACKS[MORNING])
