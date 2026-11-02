from __future__ import annotations

import random

from nora import config as config_module
from nora.config import DelayBucket, ReplyDelayConfig
from nora.pacing import compute_wait, sample_delay


def cfg(**overrides) -> ReplyDelayConfig:
    base = dict(
        enabled=True,
        buckets=[
            DelayBucket(chance=0.5, min=2, max=4),
            DelayBucket(chance=0.5, min=100, max=120),
        ],
        max_wait=600,
    )
    base.update(overrides)
    return ReplyDelayConfig(**base)


def test_disabled_means_no_wait():
    assert sample_delay(cfg(enabled=False)) == 0.0
    assert compute_wait(cfg(enabled=False), burst_age=0) == 0.0


def test_samples_land_inside_a_bucket():
    rng = random.Random(7)
    for _ in range(300):
        value = sample_delay(cfg(), rng)
        assert (2 <= value <= 4) or (100 <= value <= 120)


def test_slow_bucket_does_happen():
    rng = random.Random(3)
    values = [sample_delay(cfg(), rng) for _ in range(200)]
    assert any(value <= 4 for value in values)
    assert any(value >= 100 for value in values)


def test_chances_are_weighted():
    rng = random.Random(11)
    weighted = cfg(
        buckets=[
            DelayBucket(chance=0.95, min=1, max=2),
            DelayBucket(chance=0.05, min=100, max=101),
        ]
    )
    values = [sample_delay(weighted, rng) for _ in range(2000)]
    assert sum(1 for value in values if value < 50) > 1700


def test_zero_chance_buckets_are_ignored():
    rng = random.Random(5)
    only = cfg(
        buckets=[
            DelayBucket(chance=1.0, min=5, max=5),
            DelayBucket(chance=0.0, min=999, max=999),
        ]
    )
    assert all(sample_delay(only, rng) == 5 for _ in range(50))


def test_max_wait_caps_the_guess():
    capped = cfg(
        buckets=[DelayBucket(chance=1.0, min=300, max=300)], max_wait=600
    )
    assert compute_wait(capped, burst_age=500) == 100


def test_no_wait_once_the_cap_is_passed():
    assert compute_wait(cfg(), burst_age=601) == 0.0


def test_buckets_from_yaml_dicts_are_coerced():
    parsed = ReplyDelayConfig(
        buckets=[{"chance": 1, "min": 3, "max": 3}],  # type: ignore[list-item]
        max_wait=10,
    )
    assert isinstance(parsed.buckets[0], DelayBucket)
    assert sample_delay(parsed, random.Random(0)) == 3


def test_real_config_is_loaded():
    settings = config_module.load()
    delay = settings.chat.reply_delay
    assert delay.enabled is True
    assert len(delay.buckets) == 4
    assert all(isinstance(bucket, DelayBucket) for bucket in delay.buckets)
    assert delay.max_wait == 30
    # the quick bucket should dominate but still give him time to add a text
    assert 0.5 <= delay.buckets[0].min <= 2 and delay.buckets[0].chance >= 0.4
    assert settings.chat.debounce >= 1.5
    # typing scales with the bubble, so longer texts take longer to type
    assert settings.chat.max_delay >= 6
