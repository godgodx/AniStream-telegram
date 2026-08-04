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
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from aiohttp import ClientResponse, web
from aiohttp.abc import AbstractResolver

from anistream_telegram.config import Config
from anistream_telegram.database import Database, PlaybackSession, token_hash
from anistream.models import (
    MAX_PREFETCHED_PLAYLIST_BYTES,
    ProbeResult,
    ResolvedMedia,
)
from anistream_telegram.security import (
    AuthenticationError,
    OpaqueMediaToken,
    UnsafeUpstreamError,
    public_url_parts,
    sanitize_upstream_headers,
    validate_public_addresses,
)
from anistream_telegram.source_health import SourceHealthTracker


LOGGER = logging.getLogger(__name__)
RANGE_PATTERN = re.compile(r"^bytes=\d*-\d*$")
CONTENT_RANGE_PATTERN = re.compile(r"^bytes\s+(\d+)-(\d+)/(?:\d+|\*)$", re.IGNORECASE)
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
PROBE_MEDIA_BYTES = 64 * 1024
COLD_HLS_PROBE_SECONDS = 4.0
TRANSIENT_UPSTREAM_STATUSES = {502, 503, 504}


def incomplete_vod_media_playlist(text: str) -> bool:
    upper = text.upper()
    return "#EXTINF:" in upper and "#EXT-X-ENDLIST" not in upper


def expected_stream_bytes(status: int, headers: Any) -> int | None:
    """Return the exact advertised body size, rejecting contradictory headers."""

    content_length: int | None = None
    raw_length = str(headers.get("Content-Length", "")).strip()
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid upstream Content-Length") from exc
        if not 0 <= content_length <= 9_223_372_036_854_775_807:
            raise ValueError("invalid upstream Content-Length")

    range_length: int | None = None
    raw_range = str(headers.get("Content-Range", "")).strip()
    if raw_range:
        match = CONTENT_RANGE_PATTERN.fullmatch(raw_range)
        if match is None:
            raise ValueError("invalid upstream Content-Range")
        start, end = (int(value) for value in match.groups())
        if end < start:
            raise ValueError("invalid upstream Content-Range")
        range_length = end - start + 1
    elif status == 206:
        raise ValueError("partial upstream response is missing Content-Range")

    if (
        content_length is not None
        and range_length is not None
        and content_length != range_length
    ):
        raise ValueError("upstream length headers disagree")
    return content_length if content_length is not None else range_length


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
        clean_headers = sanitize_upstream_headers(headers)
        clean_headers["Accept-Encoding"] = "identity"
        if range_header:
            clean_headers["Range"] = range_header
        last_error: Exception | None = None
        for attempt in range(2):
            current = url
            try:
                for redirect_count in range(max_redirects + 1):
                    public_url_parts(current, self.config.media_allowed_hosts)
                    response = await self.session.get(
                        current,
                        headers=clean_headers,
                        allow_redirects=False,
                    )
                    if response.status not in {301, 302, 303, 307, 308}:
                        if (
                            response.status in TRANSIENT_UPSTREAM_STATUSES
                            and attempt == 0
                        ):
                            response.release()
                            await asyncio.sleep(0.1)
                            break
                        return response
                    location = response.headers.get("Location", "").strip()
                    if not location or redirect_count >= max_redirects:
                        response.release()
                        raise web.HTTPBadGateway(
                            text="Upstream redirect could not be followed"
                        )
                    next_url = urljoin(current, location)
                    public_url_parts(next_url, self.config.media_allowed_hosts)
                    response.release()
                    current = next_url
                else:
                    raise web.HTTPBadGateway(text="Too many upstream redirects")
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise web.HTTPBadGateway(text="Upstream request failed")


class MediaGateway:
    def __init__(
        self,
        config: Config,
        database: Database,
        source_health: SourceHealthTracker | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.tokens = OpaqueMediaToken(config.session_secret)
        self.upstream = UpstreamClient(config)
        self.source_health = source_health or SourceHealthTracker()
        self._active: dict[int, dict[str, PlaybackActivity]] = defaultdict(dict)
        self._active_lock = asyncio.Lock()
        self._first_resource_seen: dict[str, float] = {}

    async def start(self) -> None:
        await self.upstream.start()

    async def close(self) -> None:
        await self.upstream.close()

    async def probe(
        self,
        media: ResolvedMedia,
        validate_first_resource: bool = False,
    ) -> ProbeResult:
        """Probe through the gateway pool so the TLS connection remains warm."""

        expected_hls = (
            media.kind == "hls"
            or ".m3u8" in urlparse(media.url).path.casefold()
        )
        maximum = (
            MAX_PREFETCHED_PLAYLIST_BYTES if expected_hls else PROBE_MEDIA_BYTES
        )
        started = time.monotonic()
        response: ClientResponse | None = None
        valid = False
        try:
            response = await self.upstream.request(
                media.url,
                dict(media.headers),
                range_header=f"bytes=0-{maximum - 1}",
            )
            if response.status not in {200, 206}:
                return ProbeResult(False, detail=f"HTTP {response.status}")
            body = await response.content.read(maximum + 1)
            if len(body) > maximum:
                return ProbeResult(False, detail="probe response is too large")
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .casefold()
            )
            final_url = str(response.url)
            is_hls = (
                ".m3u8" in urlparse(final_url).path.casefold()
                or "mpegurl" in content_type
                or body.lstrip().startswith(b"#EXTM3U")
            )
            if is_hls:
                if not body.lstrip().startswith(b"#EXTM3U"):
                    return ProbeResult(
                        False,
                        "hls",
                        "response did not contain an HLS playlist",
                    )
                if validate_first_resource:
                    await self._probe_first_hls_resource(
                        body,
                        final_url,
                        dict(media.headers),
                    )
                complete = response.status == 200
                if response.status == 206:
                    total = (
                        response.headers.get("Content-Range", "")
                        .rpartition("/")[2]
                        .strip()
                    )
                    complete = total.isdigit() and int(total) <= len(body)
                valid = True
                return ProbeResult(
                    True,
                    "hls",
                    "valid HLS playlist and startup resource",
                    body if complete else b"",
                    final_url if complete else "",
                )
            if content_type.startswith(("text/", "image/")) or "html" in content_type:
                return ProbeResult(
                    False,
                    detail=f"unexpected content type: {content_type or 'unknown'}",
                )
            if len(body) >= 12 and body[4:8] == b"ftyp":
                valid = True
                return ProbeResult(True, "mp4", "ISO Base Media header detected")
            if content_type.startswith("video/") and len(body) >= 1024:
                valid = True
                return ProbeResult(True, "video", f"video response: {content_type}")
            return ProbeResult(
                False,
                detail="response did not look like playable media",
            )
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            UnicodeDecodeError,
            ValueError,
            web.HTTPException,
        ) as exc:
            return ProbeResult(False, detail=f"connection failed: {exc}")
        finally:
            if response is not None:
                response.release()
            self.source_health.observe(
                media.url,
                latency_seconds=time.monotonic() - started,
                success=valid,
            )

    async def _probe_first_hls_resource(
        self,
        body: bytes,
        base_url: str,
        headers: dict[str, str],
    ) -> None:
        async with asyncio.timeout(COLD_HLS_PROBE_SECONDS):
            playlist = body
            playlist_url = base_url
            text = playlist.decode("utf-8-sig")
            plain_uris = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if not plain_uris:
                raise ValueError("HLS playlist has no playable resource")
            if "#EXT-X-STREAM-INF" in text.upper():
                variant_url = urljoin(playlist_url, plain_uris[0])
                public_url_parts(
                    variant_url,
                    self.config.media_allowed_hosts,
                )
                variant = await self.upstream.request(variant_url, headers)
                try:
                    if variant.status not in {200, 206}:
                        raise ValueError(
                            f"HLS variant returned HTTP {variant.status}"
                        )
                    playlist = await variant.content.read(
                        MAX_PREFETCHED_PLAYLIST_BYTES + 1
                    )
                    if len(playlist) > MAX_PREFETCHED_PLAYLIST_BYTES:
                        raise ValueError("HLS variant playlist is too large")
                    playlist_url = str(variant.url)
                finally:
                    variant.release()
                text = playlist.decode("utf-8-sig")
                plain_uris = [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
                if not plain_uris:
                    raise ValueError("HLS variant has no media segment")

            if incomplete_vod_media_playlist(text):
                raise ValueError("HLS VOD playlist is incomplete")

            targets = [urljoin(playlist_url, plain_uris[0])]
            key_match = URI_ATTRIBUTE.search(
                next(
                    (
                        line
                        for line in text.splitlines()
                        if line.lstrip().upper().startswith("#EXT-X-KEY:")
                    ),
                    "",
                )
            )
            if key_match is not None:
                targets.insert(0, urljoin(playlist_url, key_match.group(1)))
            for target in targets:
                public_url_parts(target, self.config.media_allowed_hosts)
                resource = await self.upstream.request(
                    target,
                    headers,
                    range_header=f"bytes=0-{PROBE_MEDIA_BYTES - 1}",
                )
                try:
                    if resource.status not in {200, 206}:
                        raise ValueError(
                            f"HLS startup resource returned HTTP {resource.status}"
                        )
                    sample = await resource.content.read(PROBE_MEDIA_BYTES + 1)
                    if not sample:
                        raise ValueError("HLS startup resource is empty")
                finally:
                    resource.release()

    async def options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=CAST_CORS_HEADERS)

    async def _session_and_playback(
        self,
        request: web.Request,
        playback_id: str,
    ) -> tuple[int, PlaybackSession, str, str]:
        cast_token = request.match_info.get("cast_token", "") or request.query.get(
            "cast",
            "",
        )
        if cast_token:
            playback = await self.database.get_cast_playback(cast_token, playback_id)
            if playback is None:
                raise web.HTTPForbidden(text="Cast session is unavailable")
            return playback.telegram_user_id, playback, cast_token, "cast"
        raw_session = request.cookies.get(self.config.cookie_name, "")
        authorized = await self.database.get_media_playback(
            raw_session,
            playback_id,
        )
        if authorized is None:
            raise web.HTTPUnauthorized(text="Playback session is unavailable")
        session, playback = authorized
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
        if playback.media_kind == "hls" and not request.headers.get("Range", ""):
            prefetched = await self.database.consume_playback_manifest(
                playback.id,
                user_id,
            )
            if prefetched is not None:
                body, base_url = prefetched
                async with self._stream_slot(user_id, session_key):
                    return self._playlist_body_response(
                        playback,
                        body,
                        base_url,
                        cast_token=cast_token,
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
        token = request.match_info.get("resource_token", "") or request.query.get(
            "t",
            "",
        )
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
        request_started = time.monotonic()
        health_observed = False
        try:
            async with self._stream_slot(user_id, session_key):
                upstream = await self.upstream.request(
                    target,
                    dict(playback.media_headers),
                    range_header=range_header,
                )
                try:
                    if upstream.status not in {200, 206}:
                        failed_url = str(upstream.url or target)
                        self.source_health.observe(
                            failed_url,
                            latency_seconds=time.monotonic() - request_started,
                            success=False,
                        )
                        health_observed = True
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
                        LOGGER.info(
                            "HLS playlist fetched stage=%s playback=%s seconds=%.3f",
                            "master" if force_playlist else "variant",
                            playback.id,
                            time.monotonic() - request_started,
                        )
                        try:
                            response = await self._playlist_response(
                                playback,
                                upstream,
                                final_url,
                                cast_token=cast_token,
                            )
                        except Exception:
                            self.source_health.observe(
                                final_url,
                                latency_seconds=time.monotonic() - request_started,
                                success=False,
                            )
                            health_observed = True
                            raise
                        self.source_health.observe(
                            final_url,
                            latency_seconds=time.monotonic() - request_started,
                            success=True,
                        )
                        health_observed = True
                        return response
                    return await self._stream_response(
                        request,
                        upstream,
                        cast_enabled=bool(cast_token),
                        playback_id=playback.id,
                        target_url=final_url,
                        request_started=request_started,
                        measure_startup=(
                            content_type.startswith(("video/", "audio/"))
                            or urlparse(final_url).path.casefold().endswith(
                                (
                                    ".ts",
                                    ".m4s",
                                    ".mp4",
                                    ".aac",
                                    ".webm",
                                )
                            )
                        ),
                    )
                except Exception:
                    upstream.release()
                    raise
        except UnsafeUpstreamError as exc:
            LOGGER.warning("Blocked unsafe upstream target: %s", exc)
            raise web.HTTPBadGateway(text="The media source was rejected") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if not health_observed:
                self.source_health.observe(
                    target,
                    latency_seconds=time.monotonic() - request_started,
                    success=False,
                )
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
        return self._playlist_body_response(
            playback,
            body,
            base_url,
            cast_token=cast_token,
        )

    def _playlist_body_response(
        self,
        playback: PlaybackSession,
        body: bytes,
        base_url: str,
        *,
        cast_token: str = "",
    ) -> web.Response:
        if len(body) > MAX_PLAYLIST_INPUT_BYTES:
            raise web.HTTPBadGateway(text="Upstream playlist is too large")
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise web.HTTPBadGateway(text="Upstream playlist is not UTF-8") from exc
        if not text.lstrip().startswith("#EXTM3U"):
            raise web.HTTPBadGateway(text="Upstream response is not an HLS playlist")
        if incomplete_vod_media_playlist(text):
            raise web.HTTPBadGateway(text="Upstream VOD playlist is incomplete")

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
            if cast_token:
                path = (
                    f"/cast/{quote(cast_token, safe='')}"
                    f"/media/{playback.id}/resource/{quote(token, safe='')}"
                )
            else:
                path = f"/media/{playback.id}/resource?t={quote(token)}"
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
        playback_id: str = "",
        target_url: str = "",
        request_started: float | None = None,
        measure_startup: bool = True,
    ) -> web.StreamResponse:
        try:
            expected_bytes = expected_stream_bytes(
                upstream.status,
                upstream.headers,
            )
        except ValueError as exc:
            raise web.HTTPBadGateway(
                text="Upstream media returned invalid length metadata"
            ) from exc
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
        first_resource = bool(
            measure_startup
            and playback_id
            and playback_id not in self._first_resource_seen
        )
        if first_resource:
            self._first_resource_seen[playback_id] = time.monotonic()
            if len(self._first_resource_seen) > 4096:
                oldest = next(iter(self._first_resource_seen))
                self._first_resource_seen.pop(oldest, None)
        transfer_started = time.monotonic()
        first_byte_at: float | None = None
        transferred = 0
        client_disconnected = False
        upstream_failed = False
        try:
            while True:
                try:
                    chunk = await upstream.content.read(64 * 1024)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    upstream_failed = True
                    LOGGER.info(
                        "Upstream body failed stage=stream playback=%s error=%s",
                        playback_id,
                        type(exc).__name__,
                    )
                    break
                if not chunk:
                    break
                now = time.monotonic()
                if first_byte_at is None:
                    first_byte_at = now
                transferred += len(chunk)
                try:
                    await response.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError):
                    client_disconnected = True
                    LOGGER.debug(
                        "Client abandoned media response playback=%s",
                        playback_id,
                    )
                    break
        finally:
            upstream.release()
        if (
            not client_disconnected
            and expected_bytes is not None
            and transferred != expected_bytes
        ):
            upstream_failed = True
            LOGGER.info(
                "Upstream body length mismatch playback=%s expected=%s received=%s",
                playback_id,
                expected_bytes,
                transferred,
            )
        if target_url and (first_resource or upstream_failed):
            finished = time.monotonic()
            if first_resource and (client_disconnected or upstream_failed):
                # A failed/abandoned first segment must not consume the only
                # startup measurement for this playback.
                self._first_resource_seen.pop(playback_id, None)
            if not client_disconnected:
                self.source_health.observe(
                    target_url,
                    latency_seconds=(
                        (first_byte_at or finished)
                        - (request_started or transfer_started)
                    ),
                    success=not upstream_failed and transferred > 0,
                    bytes_transferred=transferred,
                    transfer_seconds=max(0.001, finished - transfer_started),
                )
                LOGGER.info(
                    "First media resource playback=%s ttfb_seconds=%.3f "
                    "bytes=%s transfer_seconds=%.3f success=%s",
                    playback_id,
                    (first_byte_at or finished)
                    - (request_started or transfer_started),
                    transferred,
                    finished - transfer_started,
                    not upstream_failed,
                )
        if upstream_failed and not client_disconnected:
            # Headers may already have reached Caddy/browser. force_close()
            # only disables keep-alive; aiohttp can still append a clean
            # chunked terminator when the handler returns. Abort the actual
            # downstream transport so a truncated body is observable and the
            # media client can retry it.
            response.force_close()
            transport = getattr(request, "transport", None)
            if transport is not None:
                transport.abort()
        elif not client_disconnected:
            try:
                await response.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                LOGGER.debug(
                    "Client disconnected before media EOF playback=%s",
                    playback_id,
                )
        return response
