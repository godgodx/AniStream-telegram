from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from anistream.models import (
    Catalogue,
    CatalogueVariant,
    EmbedCandidate,
    Episode,
    MediaLanguage,
    ResolvedMedia,
    SearchResult,
)
from anistream.providers import ProviderRegistry, default_providers
from anistream.resolvers import ResolverRegistry, default_resolvers
from anistream.services.media_probe import RemoteMediaProbe
from anistream.utils.http import DEFAULT_USER_AGENT, HttpClient
from anistream_telegram.limits import CapacityLimiter


def search_result_payload(item: SearchResult) -> dict[str, Any]:
    return {
        "provider_id": _text(item.provider_id, 64, "provider ID"),
        "provider_name": _text(item.provider_name, 100, "provider name"),
        "title": _text(item.title, 240, "title"),
        "url": _http_url(item.url),
    }


def variant_payload(provider_id: str, item: CatalogueVariant) -> dict[str, Any]:
    return {
        "provider_id": _text(provider_id, 64, "provider ID"),
        "name": _text(item.name, 200, "variant name"),
        "url": _http_url(item.url),
        "season": _text(item.season or "Unknown season", 100, "season"),
        "language_code": item.language.code if item.language else "",
        "language_label": item.language.label if item.language else "",
    }


def catalogue_payload(catalogue: Catalogue) -> dict[str, Any]:
    if not 1 <= len(catalogue.episodes) <= 2000:
        raise ValueError("catalogue episode count is outside accepted bounds")
    for expected, episode in enumerate(catalogue.episodes, start=1):
        if episode.number != expected:
            raise ValueError("catalogue episodes must be contiguous")
        if not 1 <= len(episode.candidates) <= 20:
            raise ValueError(f"episode {episode.number} has an invalid candidate count")
    return {
        "provider_id": _text(catalogue.provider_id, 64, "provider ID"),
        "provider_name": _text(catalogue.provider_name, 100, "provider name"),
        "title": _text(catalogue.title, 240, "title"),
        "url": _http_url(catalogue.url),
        "season": _text(catalogue.season, 100, "season"),
        "language_code": _text(catalogue.language.code, 64, "language code"),
        "language_label": _text(catalogue.language.label, 100, "language label"),
        "total_episodes": len(catalogue.episodes),
        "episodes": [
            {
                "number": episode.number,
                "candidates": [
                    {
                        "player": _text(candidate.player, 100, "player"),
                        "url": _http_url(candidate.url),
                    }
                    for candidate in episode.candidates
                ],
            }
            for episode in catalogue.episodes
        ],
    }


def _text(value: object, maximum: int, label: str) -> str:
    text = str(value).strip()
    if not text or len(text) > maximum or any(ord(character) < 32 for character in text):
        raise ValueError(f"{label} is outside accepted bounds")
    return text


def _http_url(value: object) -> str:
    url = _text(value, 8192, "URL")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be absolute HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("URL credentials are forbidden")
    return url


def catalogue_from_payload(payload: dict[str, Any]) -> Catalogue:
    episodes = tuple(
        Episode(
            int(item["number"]),
            tuple(
                EmbedCandidate(str(candidate["player"]), str(candidate["url"]))
                for candidate in item.get("candidates", [])
            ),
        )
        for item in payload["episodes"]
    )
    return Catalogue(
        provider_id=str(payload["provider_id"]),
        provider_name=str(payload["provider_name"]),
        title=str(payload["title"]),
        url=str(payload["url"]),
        season=str(payload["season"]),
        language=MediaLanguage(
            str(payload["language_code"]),
            str(payload["language_label"]),
        ),
        episodes=episodes,
    )


class CoreService:
    def __init__(self, *, user_agent: str = "", cf_clearance: str = "") -> None:
        cookie = f"cf_clearance={cf_clearance}" if cf_clearance else ""
        self.http = HttpClient(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            cookie=cookie,
            cookie_hosts={"anime-sama.to", "www.anime-sama.to"},
        )
        providers = default_providers(self.http)
        self.providers = ProviderRegistry(providers)
        self._provider_aliases = {
            provider.id: f"Provider {index}"
            for index, provider in enumerate(providers, start=1)
        }
        # Provider pages may benefit from conservative retries, but playback
        # discovery must fail over quickly. A dead embed/media host must not
        # hold the Mini App request open until the reverse proxy returns 504.
        media_http = HttpClient(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            cookie=cookie,
            cookie_hosts={"anime-sama.to", "www.anime-sama.to"},
            timeout=(5.0, 10.0),
            retry_total=0,
        )
        self.resolvers = ResolverRegistry(default_resolvers(media_http))
        self.probe = RemoteMediaProbe(media_http)
        self.provider_capacity = CapacityLimiter(
            total_limit=4,
            per_key_limit=1,
        )

    def provider_alias(self, provider_id: str) -> str:
        return self._provider_aliases.get(str(provider_id), "Provider")

    def _with_provider_alias(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "provider_alias": self.provider_alias(str(payload["provider_id"])),
        }

    async def search(
        self,
        query: str,
        *,
        actor_key: object = "internal",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async with self.provider_capacity.slot(str(actor_key)):
            results, errors = await asyncio.to_thread(self.providers.search, query)
        return [
            self._with_provider_alias(search_result_payload(item))
            for item in results
        ], errors

    async def variants(
        self,
        provider_id: str,
        url: str,
        *,
        actor_key: object = "internal",
    ) -> list[dict[str, Any]]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError("unknown provider")
        async with self.provider_capacity.slot(str(actor_key)):
            items = await asyncio.to_thread(provider.variants, url)
        return [
            self._with_provider_alias(variant_payload(provider_id, item))
            for item in items
        ]

    async def catalogue(
        self,
        provider_id: str,
        url: str,
        *,
        actor_key: object = "internal",
    ) -> dict[str, Any]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError("unknown provider")
        async with self.provider_capacity.slot(str(actor_key)):
            item = await asyncio.to_thread(provider.catalogue, url)
        return self._with_provider_alias(catalogue_payload(item))

    async def prepare_media(
        self,
        payload: dict[str, Any],
        episode_number: int,
        *,
        actor_key: object = "internal",
        preferred_source_index: int | None = None,
    ) -> ResolvedMedia:
        async with self.provider_capacity.slot(str(actor_key)):
            return await asyncio.to_thread(
                self._prepare_media_sync,
                payload,
                episode_number,
                preferred_source_index,
            )

    def _prepare_media_sync(
        self,
        payload: dict[str, Any],
        episode_number: int,
        preferred_source_index: int | None = None,
    ) -> ResolvedMedia:
        catalogue = catalogue_from_payload(payload)
        if not 1 <= episode_number <= len(catalogue.episodes):
            raise ValueError("episode is outside the catalogue")
        episode = catalogue.episodes[episode_number - 1]
        supported = [
            candidate
            for candidate in episode.candidates
            if self.resolvers.supports(candidate.url)
        ]
        if not supported:
            raise RuntimeError("all sources failed: no supported source")

        if preferred_source_index is not None:
            if not 0 <= preferred_source_index < len(supported):
                raise ValueError("source is outside the available range")
            candidate = supported[preferred_source_index]
            try:
                media = self.resolvers.resolve(candidate.url)
                probe = self.probe.probe(media)
                if not probe.valid:
                    raise ValueError(probe.detail)
                return replace(
                    media,
                    source_index=preferred_source_index,
                    source_count=len(supported),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"selected source failed: {candidate.player}: {exc}"
                ) from exc

        errors: list[str] = []
        # SourcePlanner is useful for multi-episode CLI planning, but for one
        # Mini App playback it preflights failed candidates and then retries
        # them during route selection. Resolve each supported source exactly
        # once and return the first verified playable result.
        for source_index, candidate in enumerate(supported):
            try:
                media = self.resolvers.resolve(candidate.url)
                probe = self.probe.probe(media)
                if not probe.valid:
                    raise ValueError(probe.detail)
                return replace(
                    media,
                    source_index=source_index,
                    source_count=len(supported),
                )
            except Exception as exc:
                errors.append(f"{candidate.player}: {exc}")
        raise RuntimeError("all sources failed: " + "; ".join(errors))
