from __future__ import annotations

import asyncio
import threading
from unittest.mock import Mock

import pytest

from anistream.models import (
    Catalogue,
    EmbedCandidate,
    Episode,
    MediaLanguage,
    ProbeResult,
    ResolvedMedia,
)
from anistream_telegram.core import (
    CoreService,
    catalogue_from_payload,
    catalogue_payload,
)
from anistream_telegram.limits import CapacityExceeded, CapacityLimiter


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
    monkeypatch.setattr(
        service.resolvers,
        "resolve",
        lambda url: ResolvedMedia(
            f"{url}/video.mp4",
            url,
            "Test resolver",
            {},
            "mp4",
        ),
    )
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

    def resolve(url: str) -> ResolvedMedia:
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

    monkeypatch.setattr(service.resolvers, "resolve", resolve)
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
