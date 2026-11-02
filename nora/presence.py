"""Her discord presence: on her phone while awake, idle asleep, busy sometimes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import PresenceConfig


@dataclass(frozen=True)
class PresenceState:
    status: str  # online | idle | dnd
    activity: str
    emoji: str


def _in_window(hour: int, window) -> bool:
    try:
        start = int(window[0]) % 24
        end = int(window[1]) % 24
    except (TypeError, ValueError, IndexError):
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def is_asleep(now: datetime, cfg: PresenceConfig) -> bool:
    return bool(cfg.sleep_hours) and _in_window(now.hour, cfg.sleep_hours)


def in_busy_window(now: datetime, cfg: PresenceConfig) -> bool:
    return any(_in_window(now.hour, window) for window in cfg.busy_hours or [])


def compute(now: datetime, cfg: PresenceConfig) -> PresenceState:
    """What she looks like right now.

    Sleeping beats busy: if she is meant to be asleep she shows idle, not dnd.
    """
    if not cfg.enabled:
        return PresenceState("online", "", "")
    if is_asleep(now, cfg):
        return PresenceState("idle", cfg.idle_activity, cfg.idle_emoji)
    if in_busy_window(now, cfg):
        return PresenceState("dnd", cfg.busy_activity, cfg.busy_emoji)
    return PresenceState("online", cfg.online_activity, cfg.online_emoji)
