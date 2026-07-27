from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from aiohttp import web

from anistream_telegram.config import Config
from anistream_telegram.core import CoreService
from anistream_telegram.database import Database, WebSession
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


class WebRoutes:
    def __init__(
        self,
        config: Config,
        database: Database,
        core: CoreService,
        media: MediaGateway,
    ) -> None:
        self.config = config
        self.database = database
        self.core = core
        self.media = media
        self.auth_limiter = SlidingWindowLimiter(12, 60)
        self.progress_limiter = SlidingWindowLimiter(120, 60)
        self.playback_limiter = SlidingWindowLimiter(12, 60)
        self.cast_limiter = SlidingWindowLimiter(10, 60)

    def register(self, app: web.Application) -> None:
        app.router.add_post("/api/auth/telegram", self.authenticate)
        app.router.add_get("/api/session", self.session_info)
        app.router.add_post("/api/playback", self.playback)
        app.router.add_post("/api/playback/episode", self.change_episode)
        app.router.add_post("/api/progress", self.progress)
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
        raw_session, csrf = await self.database.create_web_session(
            identity.user_id,
            launch,
            ttl_seconds=self.config.session_ttl_seconds,
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

    async def _prepare_episode(
        self,
        session: WebSession,
        catalogue: dict[str, Any],
        episode: int,
        total: int,
        start_position: float,
    ) -> dict[str, Any]:
        try:
            media = await self.core.prepare_media(
                catalogue,
                episode,
                actor_key=session.telegram_user_id,
            )
            public_url_parts(media.url, self.config.media_allowed_hosts)
        except CapacityExceeded as exc:
            raise web.HTTPTooManyRequests(
                text="Another playback request is already running"
            ) from exc
        except (UnsafeUpstreamError, ValueError, RuntimeError) as exc:
            LOGGER.info("Playback preparation failed: %s", type(exc).__name__)
            raise web.HTTPBadGateway(text="No safe playable source is currently available") from exc
        playback = await self.database.create_playback(
            session.telegram_user_id,
            catalogue,
            episode,
            media_url=media.url,
            media_headers=sanitize_upstream_headers(dict(media.headers)),
            media_kind=media.kind,
            source_name=media.resolver_name,
            ttl_seconds=self.config.playback_ttl_seconds,
        )
        # Persist the selected episode immediately. This covers a Mini App
        # being closed before the first timeupdate/pagehide event fires.
        await self.database.record_progress(
            session.telegram_user_id,
            catalogue,
            episode,
            start_position,
            0.0,
            False,
        )
        autoplay_enabled = await self.database.autoplay_enabled(
            session.telegram_user_id
        )
        return {
            "playback_id": playback.id,
            "stream_url": f"/media/{playback.id}/master",
            "kind": playback.media_kind,
            "source": playback.source_name,
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

    async def playback(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.playback_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Playback requests are too frequent")
        launch = dict(session.payload)
        catalogue = self._catalogue(session)
        episode, total = self._episode(catalogue, launch.get("episode"))
        stored_position = await self.database.episode_position(
            session.telegram_user_id,
            catalogue,
            episode,
        )
        start_position = (
            stored_position
            if stored_position > 0
            else max(0.0, float(launch.get("start_position", 0.0) or 0.0))
        )
        return web.json_response(
            await self._prepare_episode(
                session,
                catalogue,
                episode,
                total,
                start_position,
            )
        )

    async def change_episode(self, request: web.Request) -> web.Response:
        session = await self._authenticated(request)
        await self._csrf(request, session)
        if not await self.playback_limiter.allow(str(session.telegram_user_id)):
            raise web.HTTPTooManyRequests(text="Episode changes are too frequent")
        payload = await self._json(request)
        catalogue = self._catalogue(session)
        episode, total = self._episode(catalogue, payload.get("episode"))
        start_position = await self.database.episode_position(
            session.telegram_user_id,
            catalogue,
            episode,
        )
        response_payload = await self._prepare_episode(
            session,
            catalogue,
            episode,
            total,
            start_position,
        )
        launch = dict(session.payload)
        launch["episode"] = episode
        launch["start_position"] = start_position
        raw_session = request.cookies.get(self.config.cookie_name, "")
        if not await self.database.update_web_session_payload(
            raw_session,
            session.telegram_user_id,
            launch,
        ):
            raise web.HTTPUnauthorized(text="Authentication required")
        return web.json_response(response_payload)

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
        playback_id = str(payload.get("playback_id", ""))
        playback = await self.database.get_playback(
            playback_id,
            session.telegram_user_id,
        )
        if playback is None:
            raise web.HTTPForbidden(text="Playback session is invalid")
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
            raise web.HTTPBadRequest(text="Progress values are outside accepted bounds")
        reported_complete = payload.get("completed") is True
        completed = reported_complete or (
            duration >= 60 and position / max(duration, 1) >= 0.95
        )
        await self.database.record_progress(
            session.telegram_user_id,
            dict(playback.catalogue_payload),
            playback.episode,
            position,
            duration,
            completed,
        )
        return web.json_response({"ok": True, "completed": completed})

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
        return web.FileResponse(path)

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
