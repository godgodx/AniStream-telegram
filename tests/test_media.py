from __future__ import annotations

import re
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import anistream_telegram.media as media_module
from anistream_telegram.config import Config
from anistream_telegram.database import Database, PlaybackSession, utcnow
from anistream_telegram.media import (
    MAX_PLAYLIST_INPUT_BYTES,
    MAX_PLAYLIST_REFERENCES,
    MAX_PLAYLIST_TOKEN_LENGTH,
    MAX_REQUESTS_PER_PLAYBACK_SESSION,
    PLAYBACK_SESSION_IDLE_SECONDS,
    MediaGateway,
    expected_stream_bytes,
)
from anistream.models import ResolvedMedia


class FakeContent:
    def __init__(self, value: bytes) -> None:
        self.value = value

    async def read(self, maximum: int) -> bytes:
        return self.value[:maximum]


class FakeResponse:
    def __init__(self, body: bytes, url: str) -> None:
        self.content = FakeContent(body)
        self.url = url
        self.released = False

    def release(self) -> None:
        self.released = True


class ProbeResponse(FakeResponse):
    def __init__(
        self,
        body: bytes,
        url: str,
        *,
        status: int = 200,
        content_type: str = "application/vnd.apple.mpegurl",
    ) -> None:
        super().__init__(body, url)
        self.status = status
        self.headers = {"Content-Type": content_type}


class StreamingContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    async def read(self, _maximum: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


class StreamingUpstream:
    def __init__(
        self,
        *,
        status: int,
        headers: dict[str, str],
        chunks: list[bytes],
        url: str = "https://cdn.example/segment.ts",
    ) -> None:
        self.status = status
        self.headers = headers
        self.content = StreamingContent(chunks)
        self.url = url
        self.released = False

    def release(self) -> None:
        self.released = True


class DownstreamResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers
        self.writes: list[bytes] = []
        self.prepared = False
        self.eof = False
        self.force_closed = False

    async def prepare(self, _request) -> None:
        self.prepared = True

    async def write(self, chunk: bytes) -> None:
        self.writes.append(chunk)

    async def write_eof(self) -> None:
        self.eof = True

    def force_close(self) -> None:
        self.force_closed = True


async def test_stream_limit_counts_sessions_not_hls_requests(tmp_path: Path) -> None:
    gateway = MediaGateway(config(tmp_path), Database(config(tmp_path).database_url))

    async with AsyncExitStack() as stack:
        for _ in range(4):
            await stack.enter_async_context(gateway._stream_slot(123, "web:first"))
        await stack.enter_async_context(gateway._stream_slot(123, "web:second"))

        with pytest.raises(web.HTTPTooManyRequests) as error:
            async with gateway._stream_slot(123, "web:third"):
                pass
        assert "playback sessions" in error.value.text


async def test_stream_limit_keeps_per_session_request_backpressure(
    tmp_path: Path,
) -> None:
    gateway = MediaGateway(config(tmp_path), Database(config(tmp_path).database_url))

    async with AsyncExitStack() as stack:
        for _ in range(MAX_REQUESTS_PER_PLAYBACK_SESSION):
            await stack.enter_async_context(gateway._stream_slot(123, "web:first"))

        with pytest.raises(web.HTTPTooManyRequests) as error:
            async with gateway._stream_slot(123, "web:first"):
                pass
        assert "media requests" in error.value.text


async def test_idle_playback_session_slots_are_released(tmp_path: Path) -> None:
    gateway = MediaGateway(config(tmp_path), Database(config(tmp_path).database_url))

    async with gateway._stream_slot(123, "web:first"):
        pass
    async with gateway._stream_slot(123, "web:second"):
        pass
    for activity in gateway._active[123].values():
        activity.last_seen -= PLAYBACK_SESSION_IDLE_SECONDS + 1

    async with gateway._stream_slot(123, "web:third"):
        pass


def test_expected_stream_bytes_rejects_contradictory_range_metadata() -> None:
    assert expected_stream_bytes(
        206,
        {
            "Content-Length": "100",
            "Content-Range": "bytes 200-299/1000",
        },
    ) == 100
    with pytest.raises(ValueError, match="disagree"):
        expected_stream_bytes(
            206,
            {
                "Content-Length": "99",
                "Content-Range": "bytes 200-299/1000",
            },
        )


async def test_truncated_segment_is_force_closed_and_penalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = SimpleNamespace(observe=Mock())
    gateway = MediaGateway(
        config(tmp_path),
        Database(config(tmp_path).database_url),
        health,
    )
    upstream = StreamingUpstream(
        status=200,
        headers={"Content-Length": "10", "Content-Type": "video/mp2t"},
        chunks=[b"abc", b""],
    )
    # Failures must affect health even after startup already consumed its
    # first-resource measurement.
    gateway._first_resource_seen["playback-id"] = time.monotonic()
    downstream = DownstreamResponse(200, {})
    monkeypatch.setattr(
        media_module.web,
        "StreamResponse",
        lambda *, status, headers: downstream,
    )

    response = await gateway._stream_response(
        SimpleNamespace(),
        upstream,  # type: ignore[arg-type]
        playback_id="playback-id",
        target_url=str(upstream.url),
        request_started=time.monotonic(),
    )

    assert response is downstream
    assert downstream.writes == [b"abc"]
    assert downstream.force_closed is True
    assert downstream.eof is False
    assert upstream.released is True
    assert health.observe.call_args.kwargs["success"] is False


async def test_final_503_penalizes_source_health(
    tmp_path: Path,
) -> None:
    health = SimpleNamespace(observe=Mock())
    gateway = MediaGateway(
        config(tmp_path),
        Database(config(tmp_path).database_url),
        health,
    )
    upstream = StreamingUpstream(
        status=503,
        headers={},
        chunks=[],
    )
    gateway.upstream.request = AsyncMock(return_value=upstream)

    with pytest.raises(web.HTTPBadGateway) as error:
        await gateway._serve_target(
            SimpleNamespace(headers={}),
            123,
            SimpleNamespace(id="playback-id", media_headers={}),
            str(upstream.url),
            force_playlist=False,
            session_key="web:test",
        )

    assert "503" in error.value.text
    assert upstream.released is True
    assert health.observe.call_args.kwargs["success"] is False


async def test_invalid_hls_playlist_is_never_scored_as_healthy(
    tmp_path: Path,
) -> None:
    health = SimpleNamespace(observe=Mock())
    gateway = MediaGateway(
        config(tmp_path),
        Database(config(tmp_path).database_url),
        health,
    )
    upstream = ProbeResponse(
        b"<html>not a playlist</html>",
        "https://cdn.example/master.m3u8",
    )
    gateway.upstream.request = AsyncMock(return_value=upstream)

    with pytest.raises(web.HTTPBadGateway) as error:
        await gateway._serve_target(
            SimpleNamespace(headers={}),
            123,
            SimpleNamespace(
                id="playback-id",
                media_headers={},
                expires_at=utcnow() + timedelta(minutes=10),
            ),
            str(upstream.url),
            force_playlist=True,
            session_key="web:test",
        )

    assert "not an HLS playlist" in error.value.text
    assert upstream.released is True
    assert health.observe.call_count == 1
    assert health.observe.call_args.kwargs["success"] is False


def test_gateway_rejects_incomplete_vod_media_playlist(tmp_path: Path) -> None:
    gateway = MediaGateway(config(tmp_path), Database(config(tmp_path).database_url))
    playback = PlaybackSession(
        id="playback-id",
        telegram_user_id=123,
        catalogue_payload={},
        episode=1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    incomplete = (
        b"#EXTM3U\n"
        b"#EXT-X-PLAYLIST-TYPE:VOD\n"
        b"#EXTINF:10,\nsegment-001.ts\n"
    )

    with pytest.raises(web.HTTPBadGateway) as error:
        gateway._playlist_body_response(
            playback,
            incomplete,
            "https://cdn.example/path/quality.m3u8",
        )

    assert "incomplete" in (error.value.text or "")


async def test_probe_rejects_incomplete_vod_variant(tmp_path: Path) -> None:
    gateway = MediaGateway(config(tmp_path), Database(config(tmp_path).database_url))
    master = ProbeResponse(
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nquality.m3u8\n",
        "https://cdn.example/master.m3u8",
    )
    variant = ProbeResponse(
        (
            b"#EXTM3U\n"
            b"#EXT-X-PLAYLIST-TYPE:VOD\n"
            b"#EXTINF:10,\nsegment.ts\n"
        ),
        "https://cdn.example/quality.m3u8",
    )
    segment = ProbeResponse(
        b"media-bytes",
        "https://cdn.example/segment.ts",
        content_type="video/mp2t",
    )
    gateway.upstream.request = AsyncMock(
        side_effect=[master, variant, segment]
    )

    result = await gateway.probe(
        ResolvedMedia(
            "https://cdn.example/master.m3u8",
            "https://embed.example/",
            "Test",
            {},
            "hls",
        ),
        True,
    )

    assert result.valid is False
    assert "incomplete" in result.detail


def config(tmp_path: Path) -> Config:
    return Config(
        bot_token="123456789:test",
        allowed_users=(123,),
        public_base_url="https://watch.example",
        webhook_secret="w" * 32,
        session_secret="s" * 48,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        run_mode="webhook",
        cookie_secure=True,
        trusted_proxy_count=1,
        auth_max_age_seconds=300,
        session_ttl_seconds=3600,
        playback_ttl_seconds=7200,
        max_streams_per_user=2,
        anime_sama_user_agent="",
        anime_sama_cf_clearance="",
        media_allowed_hosts=frozenset(),
        log_level="INFO",
        project_root=tmp_path,
    )


async def test_hls_playlist_urls_are_opaque_and_bound(tmp_path: Path) -> None:
    database = Database(config(tmp_path).database_url)
    gateway = MediaGateway(config(tmp_path), database)
    playback = PlaybackSession(
        id="playback-id",
        telegram_user_id=123,
        catalogue_payload={},
        episode=1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    upstream = FakeResponse(
        (
            b"#EXTM3U\n"
            b"#EXT-X-START:TIME-OFFSET=120.0\n"
            b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            b"#EXTINF:10,\nsegment-001.ts\n"
            b"#EXT-X-ENDLIST\n"
        ),
        "https://cdn.example/path/master.m3u8",
    )
    response = await gateway._playlist_response(playback, upstream, str(upstream.url))
    assert upstream.released
    assert "cdn.example" not in response.text
    assert "#EXT-X-START" not in response.text
    assert "segment-001.ts" not in response.text
    assert 'URI="/media/playback-id/resource?t=' in response.text

    resource = next(
        line for line in response.text.splitlines() if line.startswith("/media/")
    )
    token = parse_qs(urlparse(resource).query)["t"][0]
    assert gateway.tokens.parse(token, "playback-id").endswith("/path/segment-001.ts")
    assert len(re.findall(r"/media/playback-id/resource", response.text)) == 2


async def test_gateway_probe_reuses_transport_and_checks_cold_hls_startup(
    tmp_path: Path,
) -> None:
    gateway = MediaGateway(config(tmp_path), Database(config(tmp_path).database_url))
    master = ProbeResponse(
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nquality.m3u8\n",
        "https://cdn.example/master.m3u8",
    )
    variant = ProbeResponse(
        b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        b"#EXTINF:10,\nsegment.ts\n#EXT-X-ENDLIST\n",
        "https://cdn.example/quality.m3u8",
    )
    key = ProbeResponse(
        b"0" * 16,
        "https://cdn.example/key.bin",
        status=206,
        content_type="application/octet-stream",
    )
    segment = ProbeResponse(
        b"segment",
        "https://cdn.example/segment.ts",
        status=206,
        content_type="video/mp2t",
    )
    gateway.upstream.request = AsyncMock(
        side_effect=[master, variant, key, segment]
    )

    result = await gateway.probe(
        ResolvedMedia(
            master.url,
            "https://embed.example/player",
            "Test",
            {},
            "hls",
        ),
        True,
    )

    assert result.valid is True
    assert result.prefetched_playlist == master.content.value
    assert gateway.upstream.request.await_count == 4
    assert all(item.released for item in (master, variant, key, segment))


async def test_master_reuses_the_prefetched_hls_manifest_once(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize((123,))
    raw_session, _ = await database.create_web_session(
        123,
        {},
        ttl_seconds=600,
    )
    body = b"#EXTM3U\n#EXTINF:10,\nsegment-001.ts\n#EXT-X-ENDLIST\n"
    playback = await database.create_playback(
        123,
        {},
        1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        ttl_seconds=600,
        prefetched_playlist=body,
        prefetched_playlist_url="https://cdn.example/path/master.m3u8",
    )
    gateway = MediaGateway(settings, database)
    gateway.upstream.request = AsyncMock(
        side_effect=AssertionError("upstream must not be requested"),
    )
    app = web.Application()
    app.router.add_get("/media/{playback_id}/master", gateway.master)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/media/{playback.id}/master",
            headers={"Cookie": f"{settings.cookie_name}={raw_session}"},
        )

        assert response.status == 200
        text = await response.text()
        assert "segment-001.ts" not in text
        assert f"/media/{playback.id}/resource?t=" in text
        gateway.upstream.request.assert_not_awaited()
        assert (
            await database.consume_playback_manifest(playback.id, 123)
            is None
        )
    finally:
        await client.close()
        await database.close()


async def test_cast_hls_playlist_propagates_grant_and_cors(tmp_path: Path) -> None:
    database = Database(config(tmp_path).database_url)
    gateway = MediaGateway(config(tmp_path), database)
    playback = PlaybackSession(
        id="playback-id",
        telegram_user_id=123,
        catalogue_payload={},
        episode=1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    upstream = FakeResponse(
        b"#EXTM3U\n#EXTINF:10,\nsegment-001.ts\n#EXT-X-ENDLIST\n",
        "https://cdn.example/path/master.m3u8",
    )

    response = await gateway._playlist_response(
        playback,
        upstream,
        str(upstream.url),
        cast_token="cast-grant",
    )

    assert "cast=cast-grant" in response.text
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Range" in response.headers["Access-Control-Allow-Headers"]


async def test_hls_audio_and_subtitle_tracks_are_rewritten(tmp_path: Path) -> None:
    database = Database(config(tmp_path).database_url)
    gateway = MediaGateway(config(tmp_path), database)
    playback = PlaybackSession(
        id="playback-id",
        telegram_user_id=123,
        catalogue_payload={},
        episode=1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    upstream = FakeResponse(
        (
            b"#EXTM3U\n"
            b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",'
            b'URI="audio/en.m3u8"\n'
            b'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="French",'
            b'URI="subs/fr.m3u8"\n'
            b'#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,'
            b'AUDIO="audio",SUBTITLES="subs"\n'
            b"video/1080p.m3u8\n"
        ),
        "https://cdn.example/path/master.m3u8",
    )

    response = await gateway._playlist_response(playback, upstream, str(upstream.url))

    assert "audio/en.m3u8" not in response.text
    assert "subs/fr.m3u8" not in response.text
    assert "video/1080p.m3u8" not in response.text
    assert len(re.findall(r"/media/playback-id/resource", response.text)) == 3


async def test_hls_rewrite_rejects_resource_amplification(tmp_path: Path) -> None:
    database = Database(config(tmp_path).database_url)
    gateway = MediaGateway(config(tmp_path), database)
    playback = PlaybackSession(
        id="playback-id",
        telegram_user_id=123,
        catalogue_payload={},
        episode=1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    # A nearly 2 MB input of one-character URIs previously amplified into a
    # response hundreds of megabytes large after signing every resource.
    repeats = (MAX_PLAYLIST_INPUT_BYTES - len(b"#EXTM3U\n")) // len(b"x\n")
    upstream = FakeResponse(
        b"#EXTM3U\n" + (b"x\n" * repeats),
        "https://cdn.example/path/master.m3u8",
    )
    assert MAX_PLAYLIST_REFERENCES < repeats

    with pytest.raises(web.HTTPBadGateway) as error:
        await gateway._playlist_response(playback, upstream, str(upstream.url))

    assert "too many resources" in error.value.text
    assert upstream.released


async def test_hls_rewrite_enforces_output_size_limit(tmp_path: Path) -> None:
    database = Database(config(tmp_path).database_url)
    gateway = MediaGateway(config(tmp_path), database)
    playback = PlaybackSession(
        id="playback-id",
        telegram_user_id=123,
        catalogue_payload={},
        episode=1,
        media_url="https://cdn.example/master.m3u8",
        media_headers={},
        media_kind="hls",
        source_name="Test",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    gateway.tokens.create = lambda *_args: "t" * MAX_PLAYLIST_TOKEN_LENGTH
    upstream = FakeResponse(
        b"#EXTM3U\n" + (b"x\n" * 600),
        "https://cdn.example/path/master.m3u8",
    )

    with pytest.raises(web.HTTPBadGateway) as error:
        await gateway._playlist_response(playback, upstream, str(upstream.url))

    assert "response size limit" in error.value.text
    assert upstream.released
