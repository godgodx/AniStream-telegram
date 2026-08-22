from __future__ import annotations

import asyncio

import pytest

from anistream_telegram.limits import (
    CapacityExceeded,
    CapacityLimiter,
    SlidingWindowLimiter,
)


async def test_sliding_window_limiter_rejects_excess_requests() -> None:
    limiter = SlidingWindowLimiter(2, 60)

    assert await limiter.allow("user") is True
    assert await limiter.allow("user") is True
    assert await limiter.allow("user") is False
    assert await limiter.allow("other-user") is True


async def test_capacity_limiter_fails_fast_and_releases_slots() -> None:
    limiter = CapacityLimiter(total_limit=1, per_key_limit=1)

    async with limiter.slot("user"):
        with pytest.raises(CapacityExceeded):
            async with limiter.slot("user"):
                pass
        with pytest.raises(CapacityExceeded):
            async with limiter.slot("other-user"):
                pass

    async with limiter.slot("other-user"):
        pass


async def test_sliding_window_limiter_keeps_recent_windows_when_bounded() -> None:
    limiter = SlidingWindowLimiter(1, 60)

    # Fill the map to its internal bound, then add one recent key. Under a
    # continued flood only the least recently active keys are evicted, so
    # the recent window must survive instead of losing every window.
    for attacker in range(10_000):
        assert await limiter.allow(f"early-{attacker}") is True

    assert await limiter.allow("recent") is True

    for attacker in range(100):
        assert await limiter.allow(f"flood-{attacker}") is True

    assert len(limiter.values) <= 10_000
    assert "recent" in limiter.values
    assert await limiter.allow("recent") is False


async def test_sliding_window_limiter_expires_stale_keys_before_eviction() -> None:
    limiter = SlidingWindowLimiter(1, 1)

    assert await limiter.allow("stale") is True
    await asyncio.sleep(1.1)
    for attacker in range(10_050):
        await limiter.allow(f"attacker-{attacker}")

    assert "stale" not in limiter.values
    assert await limiter.allow("stale") is True
