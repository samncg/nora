"""Decide when Nora should text you on her own.

Pure logic, no discord, so it can be tested deterministically.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import ProactiveConfig

# If the last thing he said was a sign off, going quiet is expected.
SIGNOFF_RE = re.compile(
    r"\b("
    r"good\s*night|gn|night night|nighty night|bye|goodbye|cya|see ya|see you|ttyl|"
    r"talk later|talk to you later|brb|afk|im out|i'm out|going to bed|going to sleep|"
    r"gonna sleep|sleep well|headed to bed|off to bed"
    r")\b",
    re.I,
)


@dataclass(frozen=True)
class Decision:
    send: bool
    reason: str


def in_quiet_hours(now: datetime, quiet: tuple[int, int] | list[int]) -> bool:
    """Quiet hours are [start, end) in local clock time, and may wrap midnight."""
    start, end = int(quiet[0]), int(quiet[1])
    if start == end:
        return False
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


def quiet_end(now: datetime, quiet: tuple[int, int] | list[int]) -> datetime:
    """When the current quiet window ends."""
    start, end = int(quiet[0]), int(quiet[1])
    candidate = now.replace(hour=end % 24, minute=0, second=0, microsecond=0)
    if start < end:
        return candidate
    # Wrapping window (e.g. 23 -> 8): it may end today or tomorrow.
    if now.hour >= start:
        return candidate + timedelta(days=1)
    return candidate


def should_follow_up(
    now: datetime,
    *,
    idle_minutes: float | None,
    last_role: str | None,
    followups_sent: int,
    sent_today: int,
    cfg: ProactiveConfig,
    last_user_text: str | None = None,
    minutes_since_followup: float | None = None,
) -> Decision:
    """Mid conversation silence: should she ask where he went?"""
    follow = cfg.followup
    if not follow.enabled:
        return Decision(False, "followups disabled")
    if not cfg.user_id:
        return Decision(False, "no user id configured")
    if last_role is None or idle_minutes is None:
        return Decision(False, "no conversation yet")
    if last_role != "assistant":
        return Decision(False, "he spoke last")
    if last_user_text and SIGNOFF_RE.search(last_user_text):
        return Decision(False, "he said goodnight")
    if in_quiet_hours(now, cfg.quiet_hours):
        return Decision(False, "quiet hours")
    if sent_today >= cfg.max_per_day:
        return Decision(False, "daily limit reached")
    if followups_sent >= follow.max_followups:
        return Decision(False, "already asked")
    if (
        minutes_since_followup is not None
        and minutes_since_followup < follow.after_minutes
    ):
        return Decision(False, "asked recently")
    if idle_minutes < follow.after_minutes:
        return Decision(False, "not gone long enough")
    if idle_minutes > follow.max_minutes:
        return Decision(False, "gone too long")
    return Decision(True, "ok")


def next_gap_seconds(cfg: ProactiveConfig, rng: random.Random | None = None) -> float:
    rng = rng or random
    low = max(1.0, float(cfg.min_gap_minutes))
    high = max(low, float(cfg.max_gap_minutes))
    return rng.uniform(low, high) * 60.0


def first_delay_seconds(cfg: ProactiveConfig, rng: random.Random | None = None) -> float:
    rng = rng or random
    base = max(1.0, float(cfg.first_delay_minutes))
    return rng.uniform(base * 0.6, base * 1.5) * 60.0


def decide(
    now: datetime,
    *,
    last_activity: datetime | None,
    last_proactive: datetime | None,
    sent_today: int,
    unanswered: int,
    cfg: ProactiveConfig,
    roll: float,
) -> Decision:
    """Should she start a conversation right now?"""
    if not cfg.enabled:
        return Decision(False, "disabled")
    if not cfg.user_id:
        return Decision(False, "no user id configured")
    if in_quiet_hours(now, cfg.quiet_hours):
        return Decision(False, "quiet hours")
    if sent_today >= cfg.max_per_day:
        return Decision(False, "daily limit reached")
    if unanswered >= cfg.max_unanswered:
        return Decision(False, "he is not answering")
    if last_activity is not None:
        idle = (now - last_activity).total_seconds() / 60.0
        if idle < cfg.idle_minutes:
            return Decision(False, "you talked recently")
    if last_proactive is not None:
        gap = (now - last_proactive).total_seconds() / 60.0
        if gap < cfg.min_gap_minutes:
            return Decision(False, "too soon since the last one")
    if roll > cfg.chance:
        return Decision(False, "coin flip")
    return Decision(True, "ok")
