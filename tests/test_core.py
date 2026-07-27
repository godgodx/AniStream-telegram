from __future__ import annotations

from anistream.models import Catalogue, EmbedCandidate, Episode, MediaLanguage
from anistream_telegram.core import (
    CoreService,
    catalogue_from_payload,
    catalogue_payload,
)


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
