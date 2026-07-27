from __future__ import annotations

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
