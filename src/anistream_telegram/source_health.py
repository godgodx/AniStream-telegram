from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse


SOURCE_HEALTH_TTL_SECONDS = 15 * 60
UNKNOWN_SOURCE_SCORE_SECONDS = 3.0
FAILURE_PENALTY_SECONDS = 8.0
EWMA_ALPHA = 0.35


def source_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


@dataclass(slots=True)
class SourceHealth:
    latency_seconds: float = UNKNOWN_SOURCE_SCORE_SECONDS
    throughput_bytes_per_second: float = 0.0
    successes: int = 0
    failures: int = 0
    updated_at: float = 0.0


class SourceHealthTracker:
    """Short-lived, process-local CDN health used only for source ordering.

    The application currently runs as one Compose replica. Keeping this data
    ephemeral avoids turning a temporary regional CDN slowdown into a durable
    preference and lets every restart begin from a neutral state.
    """

    def __init__(self, ttl_seconds: int = SOURCE_HEALTH_TTL_SECONDS) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._health: dict[str, SourceHealth] = {}
        self._bindings: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def bind(self, candidate_url: str, media_url: str) -> None:
        candidate = source_host(candidate_url)
        media = source_host(media_url)
        if not candidate or not media:
            return
        with self._lock:
            self._bindings[candidate] = (media, time.monotonic())

    def has_recent_delivery(self, url: str) -> bool:
        host = source_host(url)
        if not host:
            return False
        now = time.monotonic()
        with self._lock:
            item = self._fresh_locked(host, now)
            return bool(
                item
                and item.successes > 0
                and item.throughput_bytes_per_second > 0
            )

    def rank_urls(self, urls: list[str]) -> list[int]:
        now = time.monotonic()
        with self._lock:
            ranked = [
                (self._score_locked(source_host(url), now), index)
                for index, url in enumerate(urls)
            ]
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [index for _, index in ranked]

    def forget_urls(self, urls: list[str]) -> None:
        """Forget candidates after a complete preparation failure."""

        candidates = {source_host(url) for url in urls}
        candidates.discard("")
        with self._lock:
            media_hosts = {
                binding[0]
                for candidate in candidates
                if (binding := self._bindings.get(candidate)) is not None
            }
            for host in candidates | media_hosts:
                self._health.pop(host, None)
            for candidate in candidates:
                self._bindings.pop(candidate, None)

    def observe(
        self,
        url: str,
        *,
        latency_seconds: float,
        success: bool,
        bytes_transferred: int = 0,
        transfer_seconds: float = 0.0,
    ) -> None:
        host = source_host(url)
        if not host:
            return
        latency = max(0.001, min(60.0, float(latency_seconds)))
        throughput = 0.0
        if bytes_transferred > 0 and transfer_seconds > 0:
            throughput = min(
                1_000_000_000.0,
                bytes_transferred / max(0.001, transfer_seconds),
            )
        now = time.monotonic()
        with self._lock:
            item = self._fresh_locked(host, now) or SourceHealth()
            if item.successes or item.failures:
                item.latency_seconds = (
                    EWMA_ALPHA * latency
                    + (1.0 - EWMA_ALPHA) * item.latency_seconds
                )
            else:
                item.latency_seconds = latency
            if throughput:
                if item.throughput_bytes_per_second:
                    item.throughput_bytes_per_second = (
                        EWMA_ALPHA * throughput
                        + (1.0 - EWMA_ALPHA)
                        * item.throughput_bytes_per_second
                    )
                else:
                    item.throughput_bytes_per_second = throughput
            if success:
                item.successes = min(1000, item.successes + 1)
                item.failures = max(0, item.failures - 1)
            else:
                item.failures = min(1000, item.failures + 1)
            item.updated_at = now
            self._health[host] = item
            self._prune_locked(now)

    def _fresh_locked(self, host: str, now: float) -> SourceHealth | None:
        item = self._health.get(host)
        if item is None or now - item.updated_at > self.ttl_seconds:
            return None
        return item

    def _score_locked(self, candidate_host: str, now: float) -> float:
        if not candidate_host:
            return UNKNOWN_SOURCE_SCORE_SECONDS
        hosts = [candidate_host]
        binding = self._bindings.get(candidate_host)
        if binding is not None:
            media_host, bound_at = binding
            if now - bound_at <= self.ttl_seconds:
                hosts.append(media_host)
        samples = [
            item
            for host in hosts
            if (item := self._fresh_locked(host, now)) is not None
        ]
        if not samples:
            return UNKNOWN_SOURCE_SCORE_SECONDS
        latency = sum(item.latency_seconds for item in samples) / len(samples)
        successes = sum(item.successes for item in samples)
        failures = sum(item.failures for item in samples)
        failure_ratio = failures / max(1, successes + failures)
        throughput = max(
            (item.throughput_bytes_per_second for item in samples),
            default=0.0,
        )
        throughput_penalty = 0.0
        if throughput > 0:
            # Roughly model the time needed to obtain the first 512 KiB.
            throughput_penalty = min(4.0, (512 * 1024) / throughput)
        score = latency + failure_ratio * FAILURE_PENALTY_SECONDS + throughput_penalty
        return score if math.isfinite(score) else UNKNOWN_SOURCE_SCORE_SECONDS

    def _prune_locked(self, now: float) -> None:
        if len(self._health) <= 256 and len(self._bindings) <= 256:
            return
        self._health = {
            host: item
            for host, item in self._health.items()
            if now - item.updated_at <= self.ttl_seconds
        }
        self._bindings = {
            host: value
            for host, value in self._bindings.items()
            if now - value[1] <= self.ttl_seconds
        }
