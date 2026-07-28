from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from multiprocessing.connection import Connection
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
from anistream_telegram.source_health import SourceHealthTracker


LOGGER = logging.getLogger(__name__)
PREPARATION_DEADLINE_SECONDS = 40.0
CANDIDATE_DEADLINE_SECONDS = 12.0
RESOLVER_PROCESS_LIMIT = 2


def _send_resolver_result(
    connection: Connection,
    result: tuple[Any, ...],
) -> None:
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        # The parent already reached its deadline and closed the pipe.
        pass


def _resolver_process_entry(
    connection: Connection,
    url: str,
    user_agent: str,
    cookie: str,
) -> None:
    """Resolve one embed in a disposable process.

    Synchronous resolver code can be blocked below Python's cancellation
    boundary (notably in DNS or a provider parser). A process gives the parent
    a worker it can actually terminate when the preparation deadline expires.
    """

    try:
        http = HttpClient(
            user_agent=user_agent,
            cookie=cookie,
            cookie_hosts={"anime-sama.to", "www.anime-sama.to"},
            timeout=(3.0, 5.0),
            retry_total=0,
        )
        media = ResolverRegistry(default_resolvers(http)).resolve(url)
        _send_resolver_result(connection, ("ok", media))
    except BaseException as exc:
        _send_resolver_result(
            connection,
            (
                "error",
                type(exc).__name__,
                str(exc)[:1000],
            )
        )
    finally:
        connection.close()


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
    def __init__(
        self,
        *,
        user_agent: str = "",
        cf_clearance: str = "",
        source_health: SourceHealthTracker | None = None,
    ) -> None:
        cookie = f"cf_clearance={cf_clearance}" if cf_clearance else ""
        resolved_user_agent = user_agent or DEFAULT_USER_AGENT
        self.http = HttpClient(
            user_agent=resolved_user_agent,
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
            user_agent=resolved_user_agent,
            cookie=cookie,
            cookie_hosts={"anime-sama.to", "www.anime-sama.to"},
            timeout=(3.0, 5.0),
            retry_total=0,
        )
        self.resolvers = ResolverRegistry(default_resolvers(media_http))
        self.probe = RemoteMediaProbe(media_http)
        self.source_health = source_health or SourceHealthTracker()
        self._async_probe: (
            Callable[[ResolvedMedia, bool], Awaitable[Any]] | None
        ) = None
        self._resolver_user_agent = resolved_user_agent
        self._resolver_cookie = cookie
        self._resolver_context = multiprocessing.get_context("spawn")
        # A single user can only prepare once concurrently. Two isolated
        # resolvers still serve separate users without multiplying VPS memory
        # by the four-request provider admission limit.
        self._resolver_slots = asyncio.Semaphore(RESOLVER_PROCESS_LIMIT)
        self._resolver_processes: set[Any] = set()
        self._resolver_tasks: set[asyncio.Task[Any]] = set()
        self._resolver_closing = False
        self._resolver_close_lock = asyncio.Lock()
        self.provider_capacity = CapacityLimiter(
            total_limit=4,
            per_key_limit=1,
        )

    def set_async_probe(
        self,
        probe: Callable[[ResolvedMedia, bool], Awaitable[Any]],
    ) -> None:
        """Use the media gateway transport so its warm connection is reusable."""

        self._async_probe = probe

    async def close(self) -> None:
        async with self._resolver_close_lock:
            self._resolver_closing = True
            current = asyncio.current_task()
            tasks = tuple(
                task
                for task in self._resolver_tasks
                if task is not current and not task.done()
            )
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Resolver coroutines own their Process objects and close them in
            # finally. Only reap an orphan after every owner has finished.
            for process in tuple(self._resolver_processes):
                self._stop_resolver_process(process)
            self._resolver_processes.clear()

    async def _resolve_candidate(self, url: str) -> ResolvedMedia:
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - coroutine task invariant.
            raise RuntimeError("resolver must run inside an asyncio task")
        self._resolver_tasks.add(owner)
        try:
            if self._resolver_closing:
                raise RuntimeError("resolver service is closing")
            async with self._resolver_slots:
                if self._resolver_closing:
                    raise RuntimeError("resolver service is closing")
                receive, send = self._resolver_context.Pipe(duplex=False)
                process = self._resolver_context.Process(
                    target=_resolver_process_entry,
                    args=(
                        send,
                        url,
                        self._resolver_user_agent,
                        self._resolver_cookie,
                    ),
                    name="anistream-resolver",
                    daemon=True,
                )
                try:
                    process.start()
                    send.close()
                    self._resolver_processes.add(process)
                    while process.is_alive():
                        await asyncio.sleep(0.02)
                    if process.exitcode != 0:
                        raise RuntimeError(
                            f"resolver worker exited with code {process.exitcode}"
                        )
                    if not receive.poll(0.1):
                        raise RuntimeError("resolver worker returned no result")
                    result = receive.recv()
                    if result[0] == "ok":
                        return result[1]
                    raise RuntimeError(f"{result[1]}: {result[2]}")
                finally:
                    send.close()
                    receive.close()
                    self._stop_resolver_process(process)
                    self._resolver_processes.discard(process)
        finally:
            self._resolver_tasks.discard(owner)

    @staticmethod
    def _stop_resolver_process(process: Any) -> None:
        """Stop and reap a resolver worker without leaving an occupied slot."""

        try:
            pid = process.pid
        except ValueError:
            return
        if pid is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.5)
        else:
            process.join(timeout=0)
        try:
            process.close()
        except ValueError:
            pass

    def provider_alias(self, provider_id: str) -> str:
        return self._provider_aliases.get(str(provider_id), "Provider")

    def provider_profiles(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "provider_id": provider.id,
                "provider_alias": self.provider_alias(provider.id),
                "content_types": tuple(provider.content_types),
                "languages": tuple(provider.search_languages),
            }
            for provider in self.providers.providers
        )

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
        provider_ids: tuple[str, ...] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async with self.provider_capacity.slot(str(actor_key)):
            if provider_ids is None:
                results, errors = await asyncio.to_thread(
                    self.providers.search,
                    query,
                )
            else:
                results, errors = await asyncio.to_thread(
                    self.providers.search,
                    query,
                    provider_ids=provider_ids,
                )
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
        fallback_from_preferred: bool = False,
    ) -> ResolvedMedia:
        async with self.provider_capacity.slot(str(actor_key)):
            try:
                async with asyncio.timeout(PREPARATION_DEADLINE_SECONDS):
                    return await self._prepare_media(
                        payload,
                        episode_number,
                        preferred_source_index,
                        fallback_from_preferred,
                    )
            except TimeoutError as exc:
                self._forget_episode_health(payload, episode_number)
                raise RuntimeError("source preparation deadline exceeded") from exc

    def _forget_episode_health(
        self,
        payload: dict[str, Any],
        episode_number: int,
    ) -> None:
        try:
            catalogue = catalogue_from_payload(payload)
            episode = catalogue.episodes[episode_number - 1]
        except (IndexError, KeyError, TypeError, ValueError):
            return
        self.source_health.forget_urls(
            [candidate.url for candidate in episode.candidates]
        )

    async def _prepare_media(
        self,
        payload: dict[str, Any],
        episode_number: int,
        preferred_source_index: int | None = None,
        fallback_from_preferred: bool = False,
    ) -> ResolvedMedia:
        started = time.monotonic()
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
                if not fallback_from_preferred:
                    raise ValueError("source is outside the available range")
                preferred_source_index = None

        if preferred_source_index is None:
            source_order = self.source_health.rank_urls(
                [candidate.url for candidate in supported]
            )
        else:
            ranked = self.source_health.rank_urls(
                [candidate.url for candidate in supported]
            )
            ranked.remove(preferred_source_index)
            source_order = [preferred_source_index, *ranked]
            if not fallback_from_preferred:
                source_order = source_order[:1]

        errors: list[str] = []
        for source_index in source_order:
            candidate = supported[source_index]
            candidate_started = time.monotonic()
            resolve_seconds = 0.0
            probe_seconds = 0.0
            try:
                async with asyncio.timeout(CANDIDATE_DEADLINE_SECONDS):
                    resolve_started = time.monotonic()
                    media = await self._resolve_candidate(candidate.url)
                    resolve_seconds = time.monotonic() - resolve_started
                    self.source_health.bind(candidate.url, media.url)
                    cold_host = not self.source_health.has_recent_delivery(
                        media.url
                    )
                    if self._async_probe is None:
                        probe_started = time.monotonic()
                        probe = await asyncio.to_thread(self.probe.probe, media)
                    else:
                        probe_started = time.monotonic()
                        probe = await self._async_probe(media, cold_host)
                    probe_seconds = time.monotonic() - probe_started
                    if not probe.valid:
                        raise ValueError(probe.detail)
                elapsed = time.monotonic() - candidate_started
                self.source_health.observe(
                    candidate.url,
                    latency_seconds=elapsed,
                    success=True,
                )
                LOGGER.info(
                    "Playback source selected source_index=%s candidates=%s "
                    "resolve_seconds=%.3f probe_seconds=%.3f "
                    "candidate_seconds=%.3f total_seconds=%.3f",
                    source_index,
                    len(supported),
                    resolve_seconds,
                    probe_seconds,
                    elapsed,
                    time.monotonic() - started,
                )
                return replace(
                    media,
                    kind=probe.kind if probe.kind != "unknown" else media.kind,
                    source_index=source_index,
                    source_count=len(supported),
                    prefetched_playlist=probe.prefetched_playlist,
                    prefetched_playlist_url=probe.prefetched_playlist_url,
                )
            except Exception as exc:
                elapsed = time.monotonic() - candidate_started
                self.source_health.observe(
                    candidate.url,
                    latency_seconds=elapsed,
                    success=False,
                )
                errors.append(f"{candidate.player}: {exc}")
                LOGGER.info(
                    "Playback source rejected source_index=%s seconds=%.3f error=%s",
                    source_index,
                    elapsed,
                    type(exc).__name__,
                )
        prefix = (
            "selected source failed: "
            if preferred_source_index is not None and not fallback_from_preferred
            else "all sources failed: "
        )
        self.source_health.forget_urls(
            [candidate.url for candidate in supported]
        )
        raise RuntimeError(prefix + "; ".join(errors))
