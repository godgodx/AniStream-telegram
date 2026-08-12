from __future__ import annotations

import asyncio
import base64
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import anistream_telegram.core as core_module
from anistream.models import (
    Catalogue,
    EmbedCandidate,
    Episode,
    MediaLanguage,
    ProbeResult,
    ResolvedMedia,
)
from anistream.resolvers.hosts import VidzyResolver
from anistream.services.media_probe import MP4_PROBE_BYTES, RemoteMediaProbe
from anistream_telegram.core import (
    CoreService,
    catalogue_from_payload,
    catalogue_payload,
)
from anistream_telegram.limits import CapacityExceeded, CapacityLimiter


def _blocking_resolver_process(connection, *_args) -> None:
    connection.close()
    time.sleep(60)


def test_catalogue_round_trip_preserves_provider_candidates() -> None:
    original = Catalogue(
        provider_id="provider",
        provider_name="Provider",
        title="Title",
        url="https://provider.example/title",
        season="Season 2",
        language=MediaLanguage("vostfr", "VOSTFR"),
        episodes=(
            Episode(
                1,
                (
                    EmbedCandidate("Player 1", "https://video.example/embed/one"),
                    EmbedCandidate("Player 2", "https://video.example/embed/two"),
                ),
            ),
        ),
    )
    payload = catalogue_payload(original)
    restored = catalogue_from_payload(payload)
    assert restored == original
    assert payload["total_episodes"] == 1


def test_registered_providers_receive_stable_anonymous_aliases() -> None:
    service = CoreService()
    aliases = {
        provider.id: service.provider_alias(provider.id)
        for provider in service.providers.providers
    }
    assert aliases == {
        "anime_sama": "Provider 1",
        "french_stream": "Provider 2",
    }
    assert service.provider_alias("unknown-provider") == "Provider"
    assert service._with_provider_alias(
        {"provider_id": service.providers.providers[0].id}
    )["provider_alias"] == "Provider 1"
    assert service.provider_profiles() == (
        {
            "provider_id": "anime_sama",
            "provider_alias": "Provider 1",
            "content_types": ("Anime",),
            "languages": ("French",),
        },
        {
            "provider_id": "french_stream",
            "provider_alias": "Provider 2",
            "content_types": ("Movies", "Series", "Anime"),
            "languages": ("French",),
        },
    )


async def test_core_searches_only_the_selected_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    anime_search = Mock(return_value=[])
    french_stream_search = Mock(return_value=[])
    monkeypatch.setattr(service.providers.providers[0], "search", anime_search)
    monkeypatch.setattr(
        service.providers.providers[1],
        "search",
        french_stream_search,
    )

    assert await service.search(
        "Tokyo Ghoul",
        actor_key=123,
        provider_ids=("anime_sama",),
    ) == ([], [])

    anime_search.assert_called_once_with("Tokyo Ghoul")
    french_stream_search.assert_not_called()


async def test_core_rejects_concurrent_provider_work_for_same_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    service.provider_capacity = CapacityLimiter(total_limit=1, per_key_limit=1)
    started = threading.Event()
    release = threading.Event()

    def blocking_search(query: str):
        started.set()
        release.wait(timeout=5)
        return [], []

    monkeypatch.setattr(service.providers, "search", blocking_search)
    first = asyncio.create_task(service.search("first", actor_key=123))
    try:
        assert await asyncio.to_thread(started.wait, 2) is True
        with pytest.raises(CapacityExceeded):
            await service.search("second", actor_key=123)
    finally:
        release.set()

    assert await first == ([], [])


async def test_core_prepares_the_requested_supported_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = catalogue_payload(
        Catalogue(
            provider_id="provider",
            provider_name="Provider",
            title="Title",
            url="https://provider.example/title",
            season="Movie",
            language=MediaLanguage("vf", "VF"),
            episodes=(
                Episode(
                    1,
                    (
                        EmbedCandidate(
                            "Player 1",
                            "https://video.example/embed/one",
                        ),
                        EmbedCandidate(
                            "Player 2",
                            "https://video.example/embed/two",
                        ),
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)
    async def resolve(url: str) -> ResolvedMedia:
        return ResolvedMedia(
            f"{url}/video.mp4",
            url,
            "Test resolver",
            {},
            "mp4",
        )

    monkeypatch.setattr(service, "_resolve_candidate", resolve)
    monkeypatch.setattr(
        service.probe,
        "probe",
        lambda _media: ProbeResult(True, "mp4", "ok"),
    )

    media = await service.prepare_media(
        payload,
        1,
        actor_key=123,
        preferred_source_index=1,
    )

    assert media.embed_url == "https://video.example/embed/two"
    assert media.source_index == 1
    assert media.source_count == 2


async def test_core_automatic_source_fallback_checks_each_candidate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = catalogue_payload(
        Catalogue(
            provider_id="provider",
            provider_name="Provider",
            title="Title",
            url="https://provider.example/title",
            season="Movie",
            language=MediaLanguage("vf", "VF"),
            episodes=(
                Episode(
                    1,
                    (
                        EmbedCandidate(
                            "Slow broken player",
                            "https://video.example/embed/broken",
                        ),
                        EmbedCandidate(
                            "Working player",
                            "https://video.example/embed/working",
                        ),
                    ),
                ),
            ),
        )
    )
    calls: list[str] = []

    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)

    async def resolve(url: str) -> ResolvedMedia:
        calls.append(url)
        if url.endswith("/broken"):
            raise RuntimeError("source unavailable")
        return ResolvedMedia(
            f"{url}/video.mp4",
            url,
            "Test resolver",
            {},
            "mp4",
        )

    monkeypatch.setattr(service, "_resolve_candidate", resolve)
    monkeypatch.setattr(
        service.probe,
        "probe",
        lambda _media: ProbeResult(True, "mp4", "ok"),
    )

    media = await service.prepare_media(payload, 1, actor_key=123)

    assert calls == [
        "https://video.example/embed/broken",
        "https://video.example/embed/working",
    ]
    assert media.source_index == 1
    assert media.source_count == 2


async def test_core_automatic_source_selection_races_all_candidates_and_cancels_losers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = catalogue_payload(
        Catalogue(
            provider_id="provider",
            provider_name="Provider",
            title="Title",
            url="https://provider.example/title",
            season="Movie",
            language=MediaLanguage("vf", "VF"),
            episodes=(
                Episode(
                    1,
                    (
                        EmbedCandidate("Slow player", "https://slow.example/embed"),
                        EmbedCandidate("Working player", "https://working.example/embed"),
                        EmbedCandidate("Other slow player", "https://other.example/embed"),
                    ),
                ),
            ),
        )
    )
    started: set[str] = set()
    cancelled: set[str] = set()
    all_started = asyncio.Event()

    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)

    async def resolve(url: str) -> ResolvedMedia:
        started.add(url)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()
        if "working.example" in url:
            return ResolvedMedia(
                f"{url}/video.mp4",
                url,
                "Test resolver",
                {},
                "mp4",
            )
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(url)
            raise
        raise AssertionError("slow resolver unexpectedly resumed")

    async def probe(media: ResolvedMedia, _cold_host: bool) -> ProbeResult:
        assert "working.example" in media.url
        return ProbeResult(True, "mp4", "ok")

    monkeypatch.setattr(service, "_resolve_candidate", resolve)
    service.set_async_probe(probe)

    media = await asyncio.wait_for(
        service.prepare_media(payload, 1, actor_key=123),
        timeout=0.5,
    )

    assert started == {
        "https://slow.example/embed",
        "https://working.example/embed",
        "https://other.example/embed",
    }
    assert cancelled == {
        "https://slow.example/embed",
        "https://other.example/embed",
    }
    assert media.source_index == 1
    assert media.source_count == 3
    assert service.source_health.rank_urls(
        [
            "https://slow.example/embed",
            "https://working.example/embed",
            "https://other.example/embed",
        ]
    ) == [1, 0, 2]


async def test_queued_source_gets_its_full_candidate_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = catalogue_payload(
        Catalogue(
            provider_id="provider",
            provider_name="Provider",
            title="Title",
            url="https://provider.example/title",
            season="Movie",
            language=MediaLanguage("vf", "VF"),
            episodes=(
                Episode(
                    1,
                    tuple(
                        EmbedCandidate(
                            f"Player {index + 1}",
                            f"https://source-{index}.example/embed",
                        )
                        for index in range(5)
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)
    monkeypatch.setattr(core_module, "CANDIDATE_DEADLINE_SECONDS", 0.08)

    async def resolve(url: str) -> ResolvedMedia:
        source_index = int(url.split("source-", 1)[1].split(".", 1)[0])
        async with service._resolver_slots:
            if source_index < 3:
                await asyncio.sleep(0.06)
                raise RuntimeError("source unavailable")
            await asyncio.sleep(0.04)
            return ResolvedMedia(
                f"{url}/video.mp4",
                url,
                "Test resolver",
                {},
                "mp4",
            )

    async def probe(_media: ResolvedMedia, _cold_host: bool) -> ProbeResult:
        return ProbeResult(True, "mp4", "ok")

    monkeypatch.setattr(service, "_resolve_candidate", resolve)
    service.set_async_probe(probe)

    media = await service.prepare_media(payload, 1, actor_key=123)

    assert media.source_index in {3, 4}


async def test_core_prefers_the_same_source_then_falls_back_for_next_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = catalogue_payload(
        Catalogue(
            provider_id="provider",
            provider_name="Provider",
            title="Title",
            url="https://provider.example/title",
            season="Season 1",
            language=MediaLanguage("vf", "VF"),
            episodes=(
                Episode(
                    1,
                    (
                        EmbedCandidate(
                            "Working player",
                            "https://video.example/embed/working",
                        ),
                        EmbedCandidate(
                            "Broken preferred player",
                            "https://video.example/embed/broken",
                        ),
                    ),
                ),
            ),
        )
    )
    calls: list[str] = []
    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)

    async def resolve(url: str) -> ResolvedMedia:
        calls.append(url)
        if url.endswith("/broken"):
            raise RuntimeError("source unavailable")
        return ResolvedMedia(
            f"{url}/video.mp4",
            url,
            "Test resolver",
            {},
            "mp4",
        )

    monkeypatch.setattr(service, "_resolve_candidate", resolve)
    monkeypatch.setattr(
        service.probe,
        "probe",
        lambda _media: ProbeResult(True, "mp4", "ok"),
    )

    media = await service.prepare_media(
        payload,
        1,
        actor_key=123,
        preferred_source_index=1,
        fallback_from_preferred=True,
    )

    assert calls == [
        "https://video.example/embed/broken",
        "https://video.example/embed/working",
    ]
    assert media.source_index == 0


async def test_resolver_concurrency_is_bounded_for_container_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    monkeypatch.setattr(
        core_module,
        "_resolver_process_entry",
        _blocking_resolver_process,
    )
    resolutions = [
        asyncio.create_task(
            service._resolve_candidate(f"https://cdn.example/video-{index}.mp4")
        )
        for index in range(5)
    ]
    try:
        for _ in range(100):
            if len(service._resolver_processes) >= 3:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert len(service._resolver_processes) == 3
    finally:
        for resolution in resolutions:
            resolution.cancel()
        await asyncio.gather(*resolutions, return_exceptions=True)
        await service.close()

    assert service._resolver_processes == set()


async def test_cancelled_resolver_workers_are_terminated_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    original_target = core_module._resolver_process_entry
    monkeypatch.setattr(
        core_module,
        "_resolver_process_entry",
        _blocking_resolver_process,
    )
    try:
        # Reproduce more cancellations than the old four-thread pool could
        # survive. Each deadline must leave no live or reserved worker behind.
        for _ in range(5):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await service._resolve_candidate(
                        "https://cdn.example/video.mp4"
                    )
            assert service._resolver_processes == set()

        monkeypatch.setattr(
            core_module,
            "_resolver_process_entry",
            original_target,
        )
        media = await asyncio.wait_for(
            service._resolve_candidate("https://cdn.example/video.mp4"),
            timeout=10,
        )
        assert media.url == "https://cdn.example/video.mp4"
        assert service._resolver_processes == set()
    finally:
        await service.close()


async def test_close_waits_for_resolver_owners_before_closing_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    monkeypatch.setattr(
        core_module,
        "_resolver_process_entry",
        _blocking_resolver_process,
    )
    resolution = asyncio.create_task(
        service._resolve_candidate("https://cdn.example/video.mp4")
    )
    for _ in range(100):
        if service._resolver_processes:
            break
        await asyncio.sleep(0.01)
    assert service._resolver_processes

    await service.close()

    with pytest.raises(asyncio.CancelledError):
        await resolution
    assert service._resolver_processes == set()
    assert service._resolver_tasks == set()
    with pytest.raises(RuntimeError, match="closing"):
        await service._resolve_candidate("https://cdn.example/video.mp4")


class ProbeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, _size: int):
        yield self.body

    def close(self) -> None:
        self.closed = True


def test_media_probe_returns_a_complete_hls_manifest_for_reuse() -> None:
    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8\n"
    response = ProbeResponse(
        body,
        url="https://cdn.example/path/master.m3u8",
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
    )
    http = SimpleNamespace(get=Mock(return_value=response))
    probe = RemoteMediaProbe(http)  # type: ignore[arg-type]

    result = probe.probe(
        ResolvedMedia(
            "https://cdn.example/master.m3u8",
            "https://embed.example/",
            "Test",
            {},
            "hls",
        )
    )

    assert result.valid is True
    assert result.kind == "hls"
    assert result.prefetched_playlist == body
    assert result.prefetched_playlist_url == response.url
    assert response.closed is True


def test_media_probe_does_not_reuse_a_partial_hls_manifest() -> None:
    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8\n"
    response = ProbeResponse(
        body,
        url="https://cdn.example/path/master.m3u8",
        status=206,
        headers={
            "Content-Type": "application/vnd.apple.mpegurl",
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(body) + 100}",
        },
    )
    http = SimpleNamespace(get=Mock(return_value=response))
    probe = RemoteMediaProbe(http)  # type: ignore[arg-type]

    result = probe.probe(
        ResolvedMedia(
            "https://cdn.example/master.m3u8",
            "https://embed.example/",
            "Test",
            {},
            "hls",
        )
    )

    assert result.valid is True
    assert result.kind == "hls"
    assert result.prefetched_playlist == b""
    assert result.prefetched_playlist_url == ""
    assert response.closed is True


def test_media_probe_rejects_a_complete_hls_vod_that_is_only_an_ad() -> None:
    body = (
        b"#EXTM3U\n"
        b"#EXT-X-TARGETDURATION:2\n"
        + (b"#EXTINF:2.0,\nsegment.ts\n" * 9)
        + b"#EXT-X-ENDLIST\n"
    )
    response = ProbeResponse(
        body,
        url="https://cdn.example/path/ad.m3u8",
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
    )
    http = SimpleNamespace(get=Mock(return_value=response))
    probe = RemoteMediaProbe(http)  # type: ignore[arg-type]

    result = probe.probe(
        ResolvedMedia(
            response.url,
            "https://embed.example/",
            "Test",
            {},
            "hls",
        )
    )

    assert result.valid is False
    assert result.kind == "hls"
    assert "too short" in result.detail
    assert response.closed is True


def test_vidzy_resolver_prefers_the_obfuscated_content_source_over_the_decoy() -> None:
    embed_url = "https://vidzy.cc/embed-video.html"
    content_url = "https://v6.vidzy.cc/hls2/video/master.m3u8?token=test"
    host_key = sum(map(ord, "vidzy.cc")) & 255
    encrypted = bytes(
        byte ^ ((0x3D + index * 89 + host_key) & 255)
        for index, byte in enumerate(content_url.encode())
    )
    encoded = base64.b64encode(encrypted[::-1]).decode()
    page = (
        'var _fsvHls="https://s1.fsvid.lol/troll/master.m3u8";'
        "sources: [{src: (function(s){"
        'var h=(location&&location.hostname)||"",H=0;'
        "for(var j=0;j<h.length;j++){H=(H+h.charCodeAt(j))&255;}"
        'var b=atob(s),a=b.split("").reverse().join(""),r="";'
        "for(var i=0;i<a.length;i++){"
        "var kk=(0x3d+i*89+H)&255;"
        "r+=String.fromCharCode(a.charCodeAt(i)^kk)}"
        'return /^https?:/.test(r)?r:"https://s1.fsvid.lol/troll/master.m3u8"'
        '})("' + encoded + '")} ]'
    )
    http = SimpleNamespace(
        user_agent="Test Agent",
        get=Mock(return_value=SimpleNamespace(status_code=200, text=page)),
    )

    media = VidzyResolver(http).resolve(embed_url)  # type: ignore[arg-type]

    assert media.url == content_url
    assert media.kind == "hls"


def test_media_probe_uses_a_small_mp4_range() -> None:
    body = b"\x00\x00\x00\x18ftyp" + (b"\x00" * (MP4_PROBE_BYTES - 8))
    response = ProbeResponse(
        body,
        url="https://cdn.example/video.mp4",
        status=206,
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{MP4_PROBE_BYTES - 1}/999999",
        },
    )
    http = SimpleNamespace(get=Mock(return_value=response))
    probe = RemoteMediaProbe(http)  # type: ignore[arg-type]

    result = probe.probe(
        ResolvedMedia(
            response.url,
            "https://embed.example/",
            "Test",
            {},
            "mp4",
        )
    )

    assert result.valid is True
    assert result.kind == "mp4"
    assert result.prefetched_playlist == b""
    headers = http.get.call_args.kwargs["headers"]
    assert headers["Range"] == f"bytes=0-{MP4_PROBE_BYTES - 1}"


def _two_source_payload() -> dict:
    return catalogue_payload(
        Catalogue(
            provider_id="provider",
            provider_name="Provider",
            title="Title",
            url="https://provider.example/title",
            season="Movie",
            language=MediaLanguage("vf", "VF"),
            episodes=(
                Episode(
                    1,
                    (
                        EmbedCandidate("Player 1", "https://one.example/embed"),
                        EmbedCandidate("Player 2", "https://two.example/embed"),
                    ),
                ),
            ),
        )
    )


async def test_complete_source_failure_forgets_poisoned_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = _two_source_payload()
    urls = ["https://one.example/embed", "https://two.example/embed"]
    service.source_health.observe(urls[0], latency_seconds=8.0, success=False)
    assert service.source_health.rank_urls(urls) == [1, 0]
    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)

    async def reject(_url: str) -> ResolvedMedia:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(service, "_resolve_candidate", reject)
    with pytest.raises(RuntimeError, match="all sources failed"):
        await service.prepare_media(payload, 1)

    assert service.source_health.rank_urls(urls) == [0, 1]


async def test_global_preparation_timeout_forgets_poisoned_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CoreService()
    payload = _two_source_payload()
    urls = ["https://one.example/embed", "https://two.example/embed"]
    service.source_health.observe(urls[0], latency_seconds=8.0, success=False)
    monkeypatch.setattr(service.resolvers, "supports", lambda _url: True)
    monkeypatch.setattr(core_module, "PREPARATION_DEADLINE_SECONDS", 0.01)

    async def stall(_url: str) -> ResolvedMedia:
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    monkeypatch.setattr(service, "_resolve_candidate", stall)
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        await service.prepare_media(payload, 1)

    assert service.source_health.rank_urls(urls) == [0, 1]
