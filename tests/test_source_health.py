from __future__ import annotations

from anistream_telegram.source_health import SourceHealthTracker


def test_recent_gateway_performance_reorders_bound_embed_sources() -> None:
    tracker = SourceHealthTracker()
    first = "https://embed-one.example/player/1"
    second = "https://embed-two.example/player/2"
    tracker.bind(first, "https://slow-cdn.example/master.m3u8")
    tracker.bind(second, "https://fast-cdn.example/master.m3u8")
    tracker.observe(
        "https://slow-cdn.example/segment.ts",
        latency_seconds=3.0,
        success=True,
        bytes_transferred=512 * 1024,
        transfer_seconds=3.0,
    )
    tracker.observe(
        "https://fast-cdn.example/segment.ts",
        latency_seconds=0.2,
        success=True,
        bytes_transferred=512 * 1024,
        transfer_seconds=0.2,
    )

    assert tracker.rank_urls([first, second]) == [1, 0]


def test_recent_failures_are_penalized_without_hardcoding_a_provider() -> None:
    tracker = SourceHealthTracker()
    unstable = "https://unstable.example/embed"
    unknown = "https://unknown.example/embed"
    tracker.observe(
        unstable,
        latency_seconds=0.1,
        success=False,
    )

    assert tracker.rank_urls([unstable, unknown]) == [1, 0]
