from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from anistream_telegram.bot import MAIN_MENU_TEXT, main_keyboard
from anistream_telegram.config import Config
from anistream_telegram.core import CoreService
from anistream_telegram.database import Database, PlaybackSession, WebSession
from anistream_telegram.limits import CapacityExceeded, SlidingWindowLimiter
from anistream_telegram.media import MediaGateway
from anistream_telegram.security import (
    AuthenticationError,
    UnsafeUpstreamError,
    public_url_parts,
    sanitize_upstream_headers,
    validate_telegram_init_data,
)


LOGGER = logging.getLogger(__name__)
CONFIG_KEY = web.AppKey("config", Config)
PREPARED_PLAYBACK_TTL_SECONDS = 10 * 60
HASHED_ASSET_PATTERN = re.compile(
    r"^/app/assets/[a-z0-9_-]+\.[0-9a-f]{12}(?:\.min)?\.(?:js|css)$"
)


class WebRoutes:
    def __init__(
        self,
        config: Config,
        database: Database,
        core: CoreService,
        media: MediaGateway,
        bot: Bot | None = None,
        background_tasks: set[asyncio.Task[Any]] | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.core = core
        self.media = media
        self.bot = bot
        self.background_tasks = background_tasks
        self.auth_limiter = SlidingWindowLimiter(12, 60)
        self.progress_limiter = SlidingWindowLimiter(120, 60)
        self.playback_limiter = SlidingWindowLimiter(12, 60)
        self.prefetch_limiter = SlidingWindowLimiter(4, 60)
        self.cast_limiter = SlidingWindowLimiter(10, 60)

    async def _restore_main_menu(self, chat_id: int, message_id: int) -> None:
        if self.bot is None:
            return
        try:
            await self.bot.edit_message_text(
                text=MAIN_MENU_TEXT,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=main_keyboard(),
            )
        except TelegramAPIError as exc:
            # Playback must remain available even if Telegram briefly rejects
            # or cannot deliver this purely presentational menu update.
            LOGGER.info("Main menu could not be restored after launch: %s", type(exc).__name__)

    def _schedule_main_menu_restore(self, launch: dict[str, Any]) -> None:
        chat_id = launch.pop("_menu_chat_id", None)
        message_id = launch.pop("_menu_message_id", None)
        if (
            self.bot is None
            or not isinstance(chat_id, int)
            or not isinstance(message_id, int)
        ):
            return
        task = asyncio.create_task(
            self._restore_main_menu(chat_id, message_id),
            name=f"restore-main-menu-{chat_id}-{message_id}",
        )
        if self.background_tasks is not None:
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/auth/telegram", self.authenticate)
        app.router.add_get("/api/session", self.session_info)
        app.router.add_post("/api/playback", self.playback)
        app.router.add_post("/api/playback/episode", self.change_episode)
        app.router.add_post("/api/playback/source", self.change_source)
        app.router.add_post("/api/playback/prefetch", self.prefetch_next)
        app.router.add_post("/api/playback/activate", self.activate_prefetch)
        app.router.add_post("/api/progress", self.progress)
        app.router.add_post("/api/progress/beacon", self.progress_beacon)
        app.router.add_post("/api/cast", self.cast)
        app.router.add_post("/api/logout", self.logout)
        app.router.add_get("/media/{playback_id}/master", self.media.master)
        app.router.add_get("/media/{playback_id}/resource", self.media.resource)
        app.router.add_options("/media/{playback_id}/master", self.media.options)
        app.router.add_options("/media/{playback_id}/resource", self.media.options)
        app.router.add_get("/health/live", self.live)
        app.router.add_get("/health/ready", self.ready)

        dist = self.config.project_root / "web" / "dist"
        if dist.is_dir():
            app.router.add_get("/app/", self.index)
            app.router.add_static("/app/", path=dist, append_version=True)
        else:
            app.router.add_get("/app/", self.frontend_missing)

    def _client_key(self, request: web.Request) -> str:
        if self.config.trusted_proxy_count > 0:
            forwarded = request.headers.get("X-Forwarded-For", "")
            addresses = [part.strip() for part in forwarded.split(",") if part.strip()]
            if len(addresses) >= self.config.trusted_proxy_count:
                return addresses[-self.config.trusted_proxy_count]
        return request.remote or "unknown"

    def _check_origin(self, request: web.Request) -> None:
        origin = request.headers.get("Origin", "")
        if origin != self.config.public_origin:
            raise web.HTTPForbidden(text="Invalid request origin")

    async def _json(self, request: web.Request) -> dict[str, Any]:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON is required")
        try:
            value = await request.json(loads=__import__("json").loads)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text="Malformed JSON") from exc
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="A JSON object is required")
        return value

    async def _authenticated(self, request: web.Request) -> WebSession:
        raw = request.cookies.get(self.config.cookie_name, "")
        session = await self.database.get_web_session(raw)
        if session is None:
            raise web.HTTPUnauthorized(text="Authentication required")
        return session

    async def _csrf(self, request: web.Request, session: WebSession) -> None:
        self._check_origin(request)
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied or not __import__("hmac").compare_digest(supplied, session.csrf_token):
            raise web.HTTPForbidden(text="CSRF validation failed")

    async def authenticate(self, request: web.Request) -> web.Response:
        started = time.monotonic()
        self._check_origin(request)
        if not await self.auth_limiter.allow(self._client_key(request)):
            raise web.HTTPTooManyRequests(text="Too many authentication attempts")
        payload = await self._json(request)
        init_data = str(payload.get("init_data", ""))
        launch_token = str(payload.get("launch_token", ""))
        if len(launch_token) > 128:
            raise web.HTTPBadRequest(text="Launch token is malformed")
        try:
            identity = validate_telegram_init_data(
                init_data,
                self.config.bot_token,
                max_age_seconds=self.config.auth_max_age_seconds,
            )
        except AuthenticationError as exc:
            LOGGER.info("Rejected Telegram Mini App authentication")
            raise web.HTTPUnauthorized(text="Telegram authentication failed") from exc
        if not await self.database.is_allowed(identity.user_id):
            raise web.HTTPForbidden(text="This Telegram account is not authorized")
        launch = await self.database.exchange_launch_ticket(launch_token, identity.user_id)
        if launch is None:
            raise web.HTTPForbidden(text="Launch link is invalid or expired")
        menu_chat_id = launch.get("_menu_chat_id")
        menu_message_id = launch.get("_menu_message_id")
        launch.pop("_menu_chat_id", None)
        launch.pop("_menu_message_id", None)
        raw_session, csrf = await self.database.create_web_session(
            identity.user_id,
            launch,
            ttl_seconds=self.config.session_ttl_seconds,
        )
        self._schedule_main_menu_restore(
            {
                "_menu_chat_id": menu_chat_id,
                "_menu_message_id": menu_message_id,
            }
        )
        response = web.json_response(
            {
                "ok": True,
                "csrf_token": csrf,
                "user": {
                    "id": identity.user_id,
                    "first_name": identity.first_name,
                },
            }
        )
        response.set_cookie(
            self.config.cookie_name,
            raw_session,
            max_age=self.config.session_ttl_seconds,
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="None" if self.config.cookie_secure else "Lax",
            path="/",
        )
        LOGGER.info(
            "Mini App authentication completed user=%s seconds=%.3f",
            identity.user_id,
            time.monotonic() - started,
        )
        return response

    @staticmethod
    def _catalogue(session: WebSession) -> dict[str, Any]:
        catalogue = session.payload.get("catalogue")
        if not isinstance(catalogue, dict):
            raise web.HTTPBadRequest(text="Playback catalogue is missing")
        return catalogue

    @staticmethod
    def _episode(catalogue: dict[str, Any], value: Any) -> tuple[int, int]:
        try:
            episode = int(value)
            total = int(catalogue["total_episodes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="Playback metadata is malformed") from exc
        if not 1 <= episode <= total:
            raise web.HTTPBadRequest(text="Episode is outside the catalogue")
        return episode, total

    @staticmethod
    def _source_index(value: Any) -> int | None:
        if value is None:
            return None
        try:
            source_index = int(value)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="Source selection is malformed") from exc
        if not 0 <= source_index < 20:
            raise web.HTTPBadRequest(text="Source is outside the available range")
        return source_index

    async def _prepare_episode(
        self,
        session: WebSession,
        catalogue: dict[str, Any],
        episode: int,
        total: int,
        start_position: float,
        *,
        preferred_source_index: int | None = None,
        fallback_from_preferred: bool = False,
        record_progress: bool = True,
        prepared: bool = False,
        actor_key: object | None = None,
    ) -> dict[str, Any]:
        preparation_started = time.monotonic()
        try:
            media = await self.core.prepare_media(
                catalogue,
                episode,
                actor_key=(
                    session.telegram_user_id
                    if actor_key is None
                    else actor_key
                ),
                preferred_source_index=preferred_source_index,
                fallback_from_preferred=fallback_from_preferred,
            )
            public_url_parts(media.url, self.config.media_allowed_hosts)
            prefetched_playlist_url = str(
                getattr(media, "prefetched_playlist_url", "")
            )
            if prefetched_playlist_url:
                public_url_parts(
                    prefetched_playlist_url,
                    self.config.media_allowed_hosts,
                )
        except CapacityExceeded as exc:
            raise web.HTTPTooManyRequests(
                text="Another playback request is already running"
            ) from exc
        except (UnsafeUpstreamError, ValueError, RuntimeError) as exc:
            LOGGER.info("Playback preparation failed: %s", type(exc).__name__)
            raise web.HTTPBadGateway(text="No safe playable source is currently available") from exc
        source_count = max(1, min(20, int(getattr(media, "source_count", 1))))
        resolved_at = time.monotonic()
        source_index = max(
            0,
            min(source_count - 1, int(getattr(media, "source_index", 0))),
        )
        ttl_seconds = (
            min(PREPARED_PLAYBACK_TTL_SECONDS, self.config.playback_ttl_seconds)
            if prepared
            else self.config.playback_ttl_seconds
        )
        playback = await self.database.create_playback(
            session.telegram_user_id,
            catalogue,
            episode,
            media_url=media.url,
            media_headers=sanitize_upstream_headers(dict(media.headers)),
            media_kind=media.kind,
            source_name=media.resolver_name,
            ttl_seconds=ttl_seconds,
            prefetched_playlist=bytes(
                getattr(media, "prefetched_playlist", b"")
            ),
            prefetched_playlist_url=prefetched_playlist_url,
            prepared=prepared,
            preferred_source_index=preferred_source_index or 0,
            source_index=source_index,
            source_count=source_count,
        )
        created_at = time.monotonic()
        LOGGER.info(
            "Playback prepared user=%s episode=%s resolve_probe_seconds=%.3f "
            "create_playback_seconds=%.3f total_seconds=%.3f prepared=%s",
            session.telegram_user_id,
            episode,
            resolved_at - preparation_started,
            created_at - resolved_at,
            created_at - preparation_started,
            prepared,
        )
        # Persist the selected episode immediately. This covers a Mini App
        # being closed before the first timeupdate/pagehide event fires.
        effective_start_position = start_position
        if record_progress:
            # Source preparation can take several seconds. The previously
            # active player may save newer progress while it is running, so
            # never write the position captured before preparation back over
            # that fresher value.
            stored_position = await self.database.saved_episode_position(
                session.telegram_user_id,
                catalogue,
                episode,
            )
            effective_start_position = (
                0.0 if stored_position is None else stored_position
            )
            await self.database.record_progress(
                session.telegram_user_id,
                catalogue,
                episode,
                effective_start_position,
                0.0,
                False,
            )
        return await self._playback_payload(
            session,
            catalogue,
            playback,
            episode,
            total,
            effective_start_position,
            source_index,
            source_count,
        )

    async def _playback_payload(
        self,
        session: WebSession,
        catalogue: dict[str, Any],
        playback: PlaybackSession,
        episode: int,
        total: int,
        start_position: float,
        source_index: int,
        source_count: int,
    ) -> dict[str, Any]:
        autoplay_enabled = await self.database.autoplay_enabled(
            session.telegram_user_id
        )
        return {
            "playback_id": playback.id,
            "playback_generation": max(
                0,
                int(getattr(playback, "generation", 0)),
            ),
            "stream_url": f"/media/{playback.id}/master",
            "kind": playback.media_kind,
            "source": playback.source_name,
            "source_index": source_index,
            "source_count": source_count,
            "has_alternative_source": source_count > 1,
            "episode": episode,
            "total_episodes": total,
            "has_previous": episode > 1,
            "has_next": episode < total,
            "autoplay_enabled": autoplay_enabled,
            "start_position": max(0.0, float(start_position or 0.0)),
            "title": str(catalogue.get("title", ""))[:200],
            "season": str(catalogue.get("season", ""))[:100],
            "language": str(catalogue.get("language_label", ""))[:100],
        }

    async def _update_session_playback(
        self,
        request: web.Request,
        session: WebSession,
        *,
        episode: int,
        start_position: float,
        source_index: int,
    ) -> None:
        launch = dict(session.payload)
        launch["episode"] = episode
        launch["start_position"] = start_position
        launch["source_index"] = source_index
        raw_session = request.cookies.get(self.config.cookie_name, "")
        if not await self.database.update_web_session_payload(
            raw_session,
            session.telegram_user_id,
            launch,
        ):
            raise web.HTTPUnauthorized(text="Authentication required")

    async def playback(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.playback_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Playback requests are too frequent")
        launch = dict(session.payload)
        catalogue = self._catalogue(session)
        episode, total = self._episode(catalogue, launch.get("episode"))
        stored_position = await self.database.saved_episode_position(
            session.telegram_user_id,
            catalogue,
            episode,
        )
        # A missing row is different from a saved 0-second position. In both
        # cases playback must begin deterministically at the start rather than
        # inheriting an upstream or WebView playback offset.
        start_position = 0.0 if stored_position is None else stored_position
        response_payload = await self._prepare_episode(
            session,
            catalogue,
            episode,
            total,
            start_position,
        )
        await self._update_session_playback(
            request,
            session,
            episode=episode,
            start_position=response_payload["start_position"],
            source_index=response_payload["source_index"],
        )
        return web.json_response(response_payload)

    async def change_episode(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.playback_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Episode changes are too frequent")
        payload = await self._json(request)
        catalogue = self._catalogue(session)
        episode, total = self._episode(catalogue, payload.get("episode"))
        stored_position = await self.database.saved_episode_position(
            session.telegram_user_id,
            catalogue,
            episode,
        )
        start_position = 0.0 if stored_position is None else stored_position
        preferred_source_index = self._source_index(payload.get("source_index"))
        response_payload = await self._prepare_episode(
            session,
            catalogue,
            episode,
            total,
            start_position,
            preferred_source_index=preferred_source_index,
            fallback_from_preferred=preferred_source_index is not None,
        )
        await self._update_session_playback(
            request,
            session,
            episode=episode,
            start_position=response_payload["start_position"],
            source_index=response_payload["source_index"],
        )
        return web.json_response(response_payload)

    async def change_source(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.playback_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Source changes are too frequent")
        payload = await self._json(request)
        playback_id = str(payload.get("playback_id", ""))
        playback = await self.database.get_playback(
            playback_id,
            session.telegram_user_id,
        )
        if playback is None:
            raise web.HTTPForbidden(text="Playback session is invalid")
        catalogue = self._catalogue(session)
        episode, total = self._episode(catalogue, session.payload.get("episode"))
        if (
            playback.episode != episode
            or dict(playback.catalogue_payload) != catalogue
        ):
            raise web.HTTPForbidden(text="Playback session is no longer current")
        preferred_source_index = self._source_index(payload.get("source_index"))
        if preferred_source_index is None:
            raise web.HTTPBadRequest(text="Choose a source")
        position, duration = self._progress_values(payload)
        observed_at_ms, event_sequence = self._progress_order(
            payload,
            required=False,
        )
        playback_generation = self._playback_generation(
            payload.get("playback_generation"),
            required=False,
        )
        accepted = await self.database.record_progress(
            session.telegram_user_id,
            catalogue,
            episode,
            position,
            duration,
            False,
            observed_at_ms=observed_at_ms,
            event_sequence=event_sequence,
            playback_id=playback_id,
            playback_generation=playback_generation,
        )
        if not accepted:
            raise web.HTTPConflict(
                text="This playback was replaced by a newer player"
            )
        start_position = position
        response_payload = await self._prepare_episode(
            session,
            catalogue,
            episode,
            total,
            start_position,
            preferred_source_index=preferred_source_index,
        )
        await self._update_session_playback(
            request,
            session,
            episode=episode,
            start_position=response_payload["start_position"],
            source_index=response_payload["source_index"],
        )
        return web.json_response(response_payload)

    async def prefetch_next(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.prefetch_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Episode prefetches are too frequent")
        payload = await self._json(request)
        catalogue = self._catalogue(session)
        current_episode, total = self._episode(
            catalogue,
            session.payload.get("episode"),
        )
        target_episode, _ = self._episode(catalogue, payload.get("episode"))
        if current_episode >= total or target_episode != current_episode + 1:
            raise web.HTTPBadRequest(text="Only the next episode can be prefetched")
        preferred_source_index = self._source_index(payload.get("source_index"))
        if preferred_source_index is None:
            preferred_source_index = (
                self._source_index(session.payload.get("source_index")) or 0
            )
        response_payload = await self._prepare_episode(
            session,
            catalogue,
            target_episode,
            total,
            0.0,
            preferred_source_index=preferred_source_index,
            fallback_from_preferred=True,
            record_progress=False,
            prepared=True,
            actor_key=f"prefetch:{session.telegram_user_id}",
        )
        response_payload["prepared"] = True
        response_payload["prepared_ttl_seconds"] = min(
            PREPARED_PLAYBACK_TTL_SECONDS,
            self.config.playback_ttl_seconds,
        )
        return web.json_response(response_payload)

    async def activate_prefetch(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.playback_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Playback requests are too frequent")
        payload = await self._json(request)
        playback_id = str(payload.get("playback_id", ""))
        catalogue = self._catalogue(session)
        current_episode, total = self._episode(
            catalogue,
            session.payload.get("episode"),
        )
        if current_episode >= total:
            raise web.HTTPConflict(text="There is no next episode to activate")
        expected_episode = current_episode + 1
        current_source_index = (
            self._source_index(session.payload.get("source_index")) or 0
        )
        activated = await self.database.activate_prepared_playback(
            playback_id,
            session.telegram_user_id,
            expected_episode=expected_episode,
            expected_preferred_source_index=current_source_index,
            expected_catalogue_payload=catalogue,
            ttl_seconds=self.config.playback_ttl_seconds,
        )
        if activated is None:
            raise web.HTTPConflict(
                text="The prepared episode is stale or no longer available"
            )
        playback, prepared = activated
        stored_position = await self.database.saved_episode_position(
            session.telegram_user_id,
            catalogue,
            expected_episode,
        )
        start_position = 0.0 if stored_position is None else stored_position
        await self.database.record_progress(
            session.telegram_user_id,
            catalogue,
            expected_episode,
            start_position,
            0.0,
            False,
        )
        await self._update_session_playback(
            request,
            session,
            episode=expected_episode,
            start_position=start_position,
            source_index=prepared.source_index,
        )
        return web.json_response(
            await self._playback_payload(
                session,
                catalogue,
                playback,
                expected_episode,
                total,
                start_position,
                prepared.source_index,
                prepared.source_count,
            )
        )

    async def session_info(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        return web.json_response(
            {
                "ok": True,
                "csrf_token": session.csrf_token,
                "user_id": session.telegram_user_id,
                "autoplay_enabled": await self.database.autoplay_enabled(
                    session.telegram_user_id
                ),
            }
        )

    async def progress(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.progress_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Progress updates are too frequent")
        payload = await self._json(request)
        return web.json_response(
            await self._record_progress_payload(session, payload)
        )

    async def progress_beacon(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        self._check_origin(request)
        payload = await self._json(request)
        supplied = str(payload.pop("csrf_token", ""))
        if not supplied or not __import__("hmac").compare_digest(
            supplied,
            session.csrf_token,
        ):
            raise web.HTTPForbidden(text="CSRF validation failed")
        if not await self.progress_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Progress updates are too frequent")
        return web.json_response(
            await self._record_progress_payload(session, payload)
        )

    async def _record_progress_payload(
        self,
        session: WebSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        playback_id = str(payload.get("playback_id", ""))
        playback = await self.database.get_playback(
            playback_id,
            session.telegram_user_id,
        )
        if playback is None:
            raise web.HTTPForbidden(text="Playback session is invalid")
        position, duration = self._progress_values(payload)
        reported_complete = payload.get("completed") is True
        completed = reported_complete or (
            duration >= 60 and position / max(duration, 1) >= 0.95
        )
        observed_at_ms, event_sequence = self._progress_order(
            payload,
            required=False,
        )
        playback_generation = self._playback_generation(
            payload.get("playback_generation"),
            required=False,
        )
        accepted = await self.database.record_progress(
            session.telegram_user_id,
            dict(playback.catalogue_payload),
            playback.episode,
            position,
            duration,
            completed,
            observed_at_ms=observed_at_ms,
            event_sequence=event_sequence,
            playback_id=playback_id,
            playback_generation=playback_generation,
        )
        return {"ok": True, "accepted": accepted, "completed": completed}

    @staticmethod
    def _progress_values(payload: dict[str, Any]) -> tuple[float, float]:
        try:
            position = float(payload.get("position", 0.0))
            duration = float(payload.get("duration", 0.0))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="Progress values are invalid") from exc
        if (
            not math.isfinite(position)
            or not math.isfinite(duration)
            or position < 0
            or duration < 0
            or position > 172_800
            or duration > 172_800
            or (duration > 0 and position > duration + 60)
        ):
            raise web.HTTPBadRequest(
                text="Progress values are outside accepted bounds"
            )
        return position, duration

    @staticmethod
    def _progress_order(
        payload: dict[str, Any],
        *,
        required: bool = False,
    ) -> tuple[int | None, int | None]:
        observed_at_raw = payload.get("observed_at_ms")
        event_sequence_raw = payload.get("event_sequence")
        if required and observed_at_raw is None and event_sequence_raw is None:
            raise web.HTTPBadRequest(
                text="Progress ordering metadata is required"
            )
        if (observed_at_raw is None) != (event_sequence_raw is None):
            raise web.HTTPBadRequest(text="Progress ordering metadata is incomplete")
        observed_at_ms: int | None = None
        event_sequence: int | None = None
        if observed_at_raw is not None and event_sequence_raw is not None:
            if (
                isinstance(observed_at_raw, bool)
                or isinstance(event_sequence_raw, bool)
                or not isinstance(observed_at_raw, int)
                or not isinstance(event_sequence_raw, int)
                or observed_at_raw < 0
                or event_sequence_raw < 0
                or observed_at_raw > 9_007_199_254_740_991
                or event_sequence_raw > 9_007_199_254_740_991
            ):
                raise web.HTTPBadRequest(
                    text="Progress ordering metadata is invalid"
                )
            observed_at_ms = observed_at_raw
            event_sequence = event_sequence_raw
        return observed_at_ms, event_sequence

    @staticmethod
    def _playback_generation(
        value: Any,
        *,
        required: bool = True,
    ) -> int | None:
        if value is None and not required:
            return None
        if isinstance(value, bool):
            raise web.HTTPBadRequest(text="Playback generation is invalid")
        try:
            generation = int(value)
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(
                text="Playback generation is invalid"
            ) from exc
        if generation < 0 or generation > 9_007_199_254_740_991:
            raise web.HTTPBadRequest(text="Playback generation is invalid")
        return generation

    async def cast(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.cast_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Cast requests are too frequent")
        payload = await self._json(request)
        playback_id = str(payload.get("playback_id", ""))
        playback = await self.database.get_playback(
            playback_id,
            session.telegram_user_id,
        )
        if playback is None:
            raise web.HTTPForbidden(text="Playback session is invalid")
        grant = await self.database.create_cast_grant(
            playback,
            ttl_seconds=min(7200, self.config.playback_ttl_seconds),
        )
        content_type = (
            "application/vnd.apple.mpegurl"
            if playback.media_kind == "hls"
            else "video/mp4"
        )
        return web.json_response(
            {
                "url": (
                    f"{self.config.public_base_url}/media/{playback.id}/master"
                    f"?cast={grant}"
                ),
                "content_type": content_type,
                "kind": playback.media_kind,
                "episode": playback.episode,
                "title": str(playback.catalogue_payload.get("title", ""))[:200],
            }
        )

    async def logout(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        raw = request.cookies.get(self.config.cookie_name, "")
        await self.database.delete_web_session(raw)
        response = web.json_response({"ok": True})
        response.del_cookie(
            self.config.cookie_name,
            path="/",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="None" if self.config.cookie_secure else "Lax",
        )
        return response

    async def index(self, request: web.Request) -> web.FileResponse:
        path = self.config.project_root / "web" / "dist" / "index.html"
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "no-cache, no-store"
        return response

    async def frontend_missing(self, request: web.Request) -> web.Response:
        return web.Response(
            text="Mini App assets are not built. Run npm install and npm run build in web/.",
            status=503,
        )

    async def live(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ready(self, request: web.Request) -> web.Response:
        try:
            await self.database.list_allowed()
        except Exception as exc:
            raise web.HTTPServiceUnavailable(text="database unavailable") from exc
        return web.json_response({"status": "ready"})


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    if HASHED_ASSET_PATTERN.fullmatch(request.path):
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable",
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "remote-playback=(self)",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' https://telegram.org https://www.gstatic.com; "
        "style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://telegram.org https://*.telegram.org",
    )
    if request.app[CONFIG_KEY].cookie_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@web.middleware
async def error_boundary(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("Unhandled web request error")
        raise web.HTTPInternalServerError(text="Internal server error")
