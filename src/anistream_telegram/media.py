from __future__ import annotations

import asyncio
import io
import logging
import re
import socket
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

import aiohttp
from aiohttp import ClientResponse, web
from aiohttp.abc import AbstractResolver

from anistream_telegram.config import Config
from anistream_telegram.database import Database, PlaybackSession, token_hash
from anistream_telegram.security import (
    AuthenticationError,
    OpaqueMediaToken,
    UnsafeUpstreamError,
    public_url_parts,
    sanitize_upstream_headers,
    validate_public_addresses,
)


LOGGER = logging.getLogger(__name__)
RANGE_PATTERN = re.compile(r"^bytes=\d*-\d*$")
URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')
PLAYLIST_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}
CAST_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Accept-Encoding, Range",
    "Access-Control-Expose-Headers": (
        "Accept-Ranges, Content-Length, Content-Range, Content-Type"
    ),
}
MAX_PLAYLIST_INPUT_BYTES = 2_000_000
MAX_PLAYLIST_OUTPUT_BYTES = 4_000_000
MAX_PLAYLIST_REFERENCES = 8_192
MAX_PLAYLIST_URI_LENGTH = 8_192
MAX_PLAYLIST_TOKEN_LENGTH = 8_192
MAX_REQUESTS_PER_PLAYBACK_SESSION = 12
PLAYBACK_SESSION_IDLE_SECONDS = 30


@dataclass(slots=True)
class PlaybackActivity:
    active_requests: int
    last_seen: float


class SafeResolver(AbstractResolver):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self.allowed_hosts = allowed_hosts

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_UNSPEC,
    ) -> list[dict[str, Any]]:
        public_url_parts(
            f"https://{host}:{port or 443}/",
            self.allowed_hosts,
        )
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host,
                port,
                family=family,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise OSError(f"DNS resolution failed for {host}") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        validate_public_addresses(host, addresses)
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for family_value, socktype, protocol, _, address in records:
            ip = address[0]
            key = (ip, family_value)
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": family_value,
                    "proto": protocol,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        return output

    async def close(self) -> None:
        return None


class UpstreamClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=45)
        connector = aiohttp.TCPConnector(
            resolver=SafeResolver(self.config.media_allowed_hosts),
            use_dns_cache=False,
            limit=50,
            limit_per_host=12,
            enable_cleanup_closed=True,
        )
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            trust_env=False,
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def request(
        self,
        url: str,
        headers: dict[str, str],
        *,
        range_header: str = "",
        max_redirects: int = 3,
    ) -> ClientResponse:
        if self.session is None:
            raise RuntimeError("upstream client is not started")
        current = url
        clean_headers = sanitize_upstream_headers(headers)
        clean_headers["Accept-Encoding"] = "identity"
        if range_header:
            clean_headers["Range"] = range_header
        for redirect_count in range(max_redirects + 1):
            public_url_parts(current, self.config.media_allowed_hosts)
            response = await self.session.get(
                current,
                headers=clean_headers,
                allow_redirects=False,
            )
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location", "").strip()
            if not location or redirect_count >= max_redirects:
                response.release()
                raise web.HTTPBadGateway(text="Upstream redirect could not be followed")
            next_url = urljoin(current, location)
            public_url_parts(next_url, self.config.media_allowed_hosts)
            response.release()
            current = next_url
        raise web.HTTPBadGateway(text="Too many upstream redirects")


class MediaGateway:
    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self.tokens = OpaqueMediaToken(config.session_secret)
        self.upstream = UpstreamClient(config)
        self._active: dict[int, dict[str, PlaybackActivity]] = defaultdict(dict)
        self._active_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.upstream.start()

    async def close(self) -> None:
        await self.upstream.close()

    async def options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=CAST_CORS_HEADERS)

    async def _session_and_playback(
        self,
        request: web.Request,
        playback_id: str,
    ) -> tuple[int, PlaybackSession, str, str]:
        cast_token = request.query.get("cast", "")
        if cast_token:
            playback = await self.database.get_cast_playback(cast_token, playback_id)
            if playback is None:
                raise web.HTTPForbidden(text="Cast session is unavailable")
            return playback.telegram_user_id, playback, cast_token, "cast"
        raw_session = request.cookies.get(self.config.cookie_name, "")
        session = await self.database.get_web_session(raw_session)
        if session is None:
            raise web.HTTPUnauthorized(text="Authentication required")
        playback = await self.database.get_playback(playback_id, session.telegram_user_id)
        if playback is None:
            raise web.HTTPForbidden(text="Playback session is unavailable")
        return (
            session.telegram_user_id,
            playback,
            "",
            f"web:{token_hash(raw_session)}",
        )

    @asynccontextmanager
    async def _stream_slot(self, user_id: int, session_key: str):
        async with self._active_lock:
            now = time.monotonic()
            sessions = self._active[user_id]
            stale = [
                key
                for key, activity in sessions.items()
                if activity.active_requests == 0
                and now - activity.last_seen >= PLAYBACK_SESSION_IDLE_SECONDS
            ]
            for key in stale:
                sessions.pop(key, None)
            activity = sessions.get(session_key)
            if (
                activity is None
                and len(sessions) >= self.config.max_streams_per_user
            ):
                raise web.HTTPTooManyRequests(
                    text="Too many simultaneous playback sessions for this account"
                )
            if activity is None:
                activity = PlaybackActivity(active_requests=0, last_seen=now)
                sessions[session_key] = activity
            if activity.active_requests >= MAX_REQUESTS_PER_PLAYBACK_SESSION:
                raise web.HTTPTooManyRequests(
                    text="Too many simultaneous media requests for this playback session"
                )
            activity.active_requests += 1
            activity.last_seen = now
        try:
            yield
        finally:
            async with self._active_lock:
                activity = self._active.get(user_id, {}).get(session_key)
                if activity is not None:
                    activity.active_requests = max(0, activity.active_requests - 1)
                    activity.last_seen = time.monotonic()

    async def master(self, request: web.Request) -> web.StreamResponse:
        playback_id = request.match_info["playback_id"]
        user_id, playback, cast_token, session_key = await self._session_and_playback(
            request,
            playback_id,
        )
        return await self._serve_target(
            request,
            user_id,
            playback,
            playback.media_url,
            force_playlist=playback.media_kind == "hls",
            cast_token=cast_token,
            session_key=session_key,
        )

    async def resource(self, request: web.Request) -> web.StreamResponse:
        playback_id = request.match_info["playback_id"]
        user_id, playback, cast_token, session_key = await self._session_and_playback(
            request,
            playback_id,
        )
        token = request.query.get("t", "")
        try:
            target = self.tokens.parse(token, playback_id)
        except AuthenticationError as exc:
            raise web.HTTPForbidden(text="Invalid media resource") from exc
        return await self._serve_target(
            request,
            user_id,
            playback,
            target,
            force_playlist=False,
            cast_token=cast_token,
            session_key=session_key,
        )

    async def _serve_target(
        self,
        request: web.Request,
        user_id: int,
        playback: PlaybackSession,
        target: str,
        *,
        force_playlist: bool,
        cast_token: str = "",
        session_key: str,
    ) -> web.StreamResponse:
        range_header = request.headers.get("Range", "")
        if range_header and not RANGE_PATTERN.fullmatch(range_header):
            raise web.HTTPRequestRangeNotSatisfiable()
        try:
            async with self._stream_slot(user_id, session_key):
                upstream = await self.upstream.request(
                    target,
                    dict(playback.media_headers),
                    range_header=range_header,
                )
                try:
                    if upstream.status not in {200, 206}:
                        raise web.HTTPBadGateway(
                            text=f"Upstream media returned HTTP {upstream.status}"
                        )
                    content_type = upstream.headers.get("Content-Type", "").split(";", 1)[
                        0
                    ].casefold()
                    final_url = str(upstream.url)
                    is_playlist = (
                        content_type in PLAYLIST_TYPES
                        or ".m3u8" in final_url.casefold()
                        or force_playlist
                    ) and not range_header
                    if is_playlist:
                        return await self._playlist_response(
                            playback,
                            upstream,
                            final_url,
                            cast_token=cast_token,
                        )
                    return await self._stream_response(
                        request,
                        upstream,
                        cast_enabled=bool(cast_token),
                    )
                except Exception:
                    upstream.release()
                    raise
        except UnsafeUpstreamError as exc:
            LOGGER.warning("Blocked unsafe upstream target: %s", exc)
            raise web.HTTPBadGateway(text="The media source was rejected") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.info("Upstream media request failed: %s", type(exc).__name__)
            raise web.HTTPBadGateway(text="The media source is temporarily unavailable") from exc

    async def _playlist_response(
        self,
        playback: PlaybackSession,
        upstream: ClientResponse,
        base_url: str,
        *,
        cast_token: str = "",
    ) -> web.Response:
        body = await upstream.content.read(MAX_PLAYLIST_INPUT_BYTES + 1)
        upstream.release()
        if len(body) > MAX_PLAYLIST_INPUT_BYTES:
            raise web.HTTPBadGateway(text="Upstream playlist is too large")
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise web.HTTPBadGateway(text="Upstream playlist is not UTF-8") from exc
        if not text.lstrip().startswith("#EXTM3U"):
            raise web.HTTPBadGateway(text="Upstream response is not an HLS playlist")

        expires_at = int(playback.expires_at.timestamp())
        reference_count = 0

        def route(uri: str) -> str:
            nonlocal reference_count
            reference_count += 1
            if reference_count > MAX_PLAYLIST_REFERENCES:
                raise web.HTTPBadGateway(
                    text="Upstream playlist contains too many resources"
                )
            clean_uri = uri.strip()
            if not clean_uri or len(clean_uri) > MAX_PLAYLIST_URI_LENGTH:
                raise web.HTTPBadGateway(
                    text="Upstream playlist contains an invalid resource URI"
                )
            absolute = urljoin(base_url, clean_uri)
            public_url_parts(absolute, self.config.media_allowed_hosts)
            token = self.tokens.create(playback.id, absolute, expires_at)
            if len(token) > MAX_PLAYLIST_TOKEN_LENGTH:
                raise web.HTTPBadGateway(
                    text="Upstream playlist contains an invalid resource URI"
                )
            path = f"/media/{playback.id}/resource?t={quote(token)}"
            if cast_token:
                path += f"&cast={quote(cast_token)}"
            return path

        output = bytearray()
        for raw_line in io.StringIO(text):
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped:
                rewritten = ""
            elif stripped.upper().startswith("#EXT-X-START:"):
                # Resume position is owned by AniStream. An upstream start
                # directive must not move a new playback away from 0 seconds.
                rewritten = ""
            elif stripped.startswith("#"):
                rewritten = URI_ATTRIBUTE.sub(
                    lambda match: f'URI="{route(match.group(1))}"',
                    line,
                )
            else:
                rewritten = route(stripped)
            encoded = (rewritten + "\n").encode("utf-8")
            if len(output) + len(encoded) > MAX_PLAYLIST_OUTPUT_BYTES:
                raise web.HTTPBadGateway(
                    text="Rewritten playlist exceeds the response size limit"
                )
            output.extend(encoded)
        headers = {"Cache-Control": "private, no-store"}
        if cast_token:
            headers.update(CAST_CORS_HEADERS)
        return web.Response(
            body=bytes(output),
            content_type="application/vnd.apple.mpegurl",
            headers=headers,
        )

    async def _stream_response(
        self,
        request: web.Request,
        upstream: ClientResponse,
        *,
        cast_enabled: bool = False,
    ) -> web.StreamResponse:
        headers: dict[str, str] = {
            "Cache-Control": "private, max-age=30",
            "X-Content-Type-Options": "nosniff",
        }
        if cast_enabled:
            headers.update(CAST_CORS_HEADERS)
        for name in (
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "Content-Type",
            "ETag",
            "Last-Modified",
        ):
            value = upstream.headers.get(name)
            if value and "\r" not in value and "\n" not in value:
                headers[name] = value
        response = web.StreamResponse(status=upstream.status, headers=headers)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            upstream.release()
        await response.write_eof()
        return response
