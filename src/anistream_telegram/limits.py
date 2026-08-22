from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class CapacityExceeded(RuntimeError):
    """Raised when provider work would exceed an active concurrency boundary."""


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self.values: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self.lock:
            values = self.values[key]
            while values and values[0] <= cutoff:
                values.popleft()
            if len(values) >= self.limit:
                return False
            values.append(now)
            if len(self.values) > 10_000:
                # Bound memory without discarding every other caller's
                # window: expire stale keys first, then evict only the
                # least recently active key until the map fits again.
                expired = [
                    key
                    for key, window in self.values.items()
                    if not window or window[-1] <= cutoff
                ]
                for key in expired:
                    del self.values[key]
                while len(self.values) > 10_000:
                    oldest = min(
                        self.values,
                        key=lambda key: self.values[key][-1],
                    )
                    del self.values[oldest]
            return True


class CapacityLimiter:
    """Fail fast instead of allowing an unbounded queue of provider work."""

    def __init__(self, total_limit: int, per_key_limit: int) -> None:
        self.total_limit = max(1, total_limit)
        self.per_key_limit = max(1, min(per_key_limit, self.total_limit))
        self._total = 0
        self._by_key: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        normalized = str(key)
        async with self._lock:
            if (
                self._total >= self.total_limit
                or self._by_key[normalized] >= self.per_key_limit
            ):
                raise CapacityExceeded("provider capacity is currently exhausted")
            self._total += 1
            self._by_key[normalized] += 1
        try:
            yield
        finally:
            async with self._lock:
                self._total = max(0, self._total - 1)
                remaining = max(0, self._by_key[normalized] - 1)
                if remaining:
                    self._by_key[normalized] = remaining
                else:
                    self._by_key.pop(normalized, None)
