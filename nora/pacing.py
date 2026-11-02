"""How long she takes to answer, like a person rather than a webhook."""

from __future__ import annotations

import random

from .config import ReplyDelayConfig


def sample_delay(cfg: ReplyDelayConfig, rng: random.Random | None = None) -> float:
    """Pick a wait from the configured buckets, weighted by their chance."""
    rng = rng or random
    if not cfg.enabled:
        return 0.0

    buckets = [bucket for bucket in cfg.buckets if bucket.chance > 0]
    if not buckets:
        return 0.0

    total = sum(bucket.chance for bucket in buckets)
    roll = rng.uniform(0.0, total)
    upto = 0.0
    for bucket in buckets:
        upto += bucket.chance
        if roll <= upto:
            low, high = sorted((max(0.0, bucket.min), max(0.0, bucket.max)))
            return rng.uniform(low, high) if high > low else low
    return 0.0


def compute_wait(
    cfg: ReplyDelayConfig,
    *,
    burst_age: float,
    rng: random.Random | None = None,
) -> float:
    """Seconds to hold off, never past `max_wait` since the first message.

    `burst_age` is how long he has been texting this burst. Sending more
    messages while she waits restarts her think time, but the cap stops her
    from being effectively unreachable.
    """
    if not cfg.enabled:
        return 0.0

    remaining = float(cfg.max_wait) - max(0.0, burst_age)
    if remaining <= 0.0:
        return 0.0
    return min(sample_delay(cfg, rng), remaining)
