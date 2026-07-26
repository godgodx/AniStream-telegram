from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from anistream_telegram.config import Config
from anistream_telegram.database import Database, PlaybackSession, utcnow
from anistream_telegram.media import MediaGateway


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
            b'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            b"#EXTINF:10,\nsegment-001.ts\n"
        ),
        "https://cdn.example/path/master.m3u8",
    )
    response = await gateway._playlist_response(playback, upstream, str(upstream.url))
    assert upstream.released
    assert "cdn.example" not in response.text
    assert "segment-001.ts" not in response.text
    assert 'URI="/media/playback-id/resource?t=' in response.text

    resource = next(
        line for line in response.text.splitlines() if line.startswith("/media/")
    )
    token = parse_qs(urlparse(resource).query)["t"][0]
    assert gateway.tokens.parse(token, "playback-id").endswith("/path/segment-001.ts")
    assert len(re.findall(r"/media/playback-id/resource", response.text)) == 2
