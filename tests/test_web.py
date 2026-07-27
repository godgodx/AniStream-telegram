from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anistream_telegram.config import Config
from anistream_telegram.database import Database
from anistream_telegram.limits import SlidingWindowLimiter
from anistream_telegram.media import MediaGateway
from anistream_telegram.web import (
    CONFIG_KEY,
    WebRoutes,
    error_boundary,
    security_headers,
)


BOT_TOKEN = "123456789:test-token"


def test_mini_app_exposes_dynamic_hls_track_controls() -> None:
    project_root = Path(__file__).resolve().parents[1]
    html = (project_root / "web" / "index.html").read_text(encoding="utf-8")
    script = (project_root / "web" / "src" / "main.js").read_text(encoding="utf-8")
    assert 'id="playback-options"' in html
    assert 'aria-controls="stream-controls"' in html
    assert 'id="quality-picker"' in html
    assert 'id="audio-picker"' in html
    assert 'id="subtitle-picker"' in html
    assert "Hls.Events.AUDIO_TRACKS_UPDATED" in script
    assert "Hls.Events.SUBTITLE_TRACKS_UPDATED" in script
    assert "hls.currentLevel = level" in script
    assert '"Unavailable"' in script
    assert "streamControlsExpanded = !streamControlsExpanded" in script
    assert 'api("/api/playback", { method: "POST" })' in script


def test_episode_picker_is_outside_native_video_controls() -> None:
    project_root = Path(__file__).resolve().parents[1]
    html = (project_root / "web" / "index.html").read_text(encoding="utf-8")
    picker_start = html.index('id="episode-picker-shell"')
    picker_end = html.index("</label>", picker_start)
    player_start = html.index('<section class="player-card"')

    assert picker_end < player_start


class FakeCore:
    def __init__(self) -> None:
        self.calls = 0

    async def prepare_media(
        self,
        catalogue: dict,
        episode: int,
        *,
        actor_key: object = "internal",
    ) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            url=f"https://cdn.example/episode-{episode}.mp4",
            headers={"Referer": "https://provider.example/"},
            kind="mp4",
            resolver_name="Test resolver",
        )


def catalogue() -> dict:
    return {
        "provider_id": "test",
        "provider_name": "Test Provider",
        "title": "Example",
        "url": "https://provider.example/catalogue/example/season-1/vf/",
        "season": "Season 1",
        "language_code": "vf",
        "language_label": "VF",
        "total_episodes": 12,
        "episodes": [],
    }


def config(tmp_path: Path) -> Config:
    return Config(
        bot_token=BOT_TOKEN,
        allowed_users=(123,),
        public_base_url="https://watch.example",
        webhook_secret="w" * 32,
        session_secret="s" * 48,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        run_mode="webhook",
        cookie_secure=True,
        trusted_proxy_count=0,
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


def signed_init_data(user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


async def test_auth_endpoint_sets_host_cookie_and_consumes_ticket(tmp_path: Path) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, None, media).register(app)  # type: ignore[arg-type]
    ticket = await database.create_launch_ticket(
        123,
        {"catalogue": {"title": "Test"}, "episode": 1},
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/auth/telegram",
            headers={"Origin": settings.public_origin},
            json={
                "init_data": signed_init_data(123),
                "launch_token": ticket,
            },
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["ok"] is True
        cookie = response.headers["Set-Cookie"]
        assert "__Host-anistream_session=" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert response.headers["Content-Security-Policy"].startswith("default-src")

        replay = await client.post(
            "/api/auth/telegram",
            headers={"Origin": settings.public_origin},
            json={
                "init_data": signed_init_data(123),
                "launch_token": ticket,
            },
        )
        assert replay.status == 403
    finally:
        await client.close()
        await database.close()


async def test_unlisted_user_cannot_authenticate_even_with_valid_init_data_and_ticket(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, None, media).register(app)  # type: ignore[arg-type]
    ticket = await database.create_launch_ticket(
        999,
        {"catalogue": {"title": "Test"}, "episode": 1},
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/auth/telegram",
            headers={"Origin": settings.public_origin},
            json={
                "init_data": signed_init_data(999),
                "launch_token": ticket,
            },
        )
        assert response.status == 403
        assert "__Host-anistream_session=" not in response.headers.get(
            "Set-Cookie",
            "",
        )
    finally:
        await client.close()
        await database.close()


async def test_auth_endpoint_rejects_wrong_origin(tmp_path: Path) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, None, media).register(app)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/auth/telegram",
            headers={"Origin": "https://evil.example"},
            json={"init_data": signed_init_data(123), "launch_token": "x"},
        )
        assert response.status == 403
    finally:
        await client.close()
        await database.close()


async def test_episode_change_resumes_position_and_updates_session(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    await database.record_progress(123, catalogue(), 4, 92.5, 1500, False)
    raw_session, csrf = await database.create_web_session(
        123,
        {
            "catalogue": catalogue(),
            "episode": 2,
            "start_position": 0,
        },
        ttl_seconds=600,
    )
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, FakeCore(), media).register(app)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/playback/episode",
            headers={
                "Origin": settings.public_origin,
                "X-CSRF-Token": csrf,
                "Cookie": f"{settings.cookie_name}={raw_session}",
            },
            json={"episode": 4},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["episode"] == 4
        assert payload["total_episodes"] == 12
        assert payload["has_previous"] is True
        assert payload["has_next"] is True
        assert payload["autoplay_enabled"] is True
        assert payload["start_position"] == 92.5

        updated = await database.get_web_session(raw_session)
        assert updated is not None
        assert updated.payload["episode"] == 4
        assert updated.payload["start_position"] == 92.5
    finally:
        await client.close()
        await database.close()


async def test_playback_and_session_expose_current_autoplay_setting(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    await database.set_autoplay_enabled(123, False)
    raw_session, csrf = await database.create_web_session(
        123,
        {
            "catalogue": catalogue(),
            "episode": 3,
            "start_position": 0,
        },
        ttl_seconds=600,
    )
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, FakeCore(), media).register(app)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        cookie = {"Cookie": f"{settings.cookie_name}={raw_session}"}
        session_response = await client.get("/api/session", headers=cookie)
        assert session_response.status == 200
        assert (await session_response.json())["autoplay_enabled"] is False

        playback_response = await client.post(
            "/api/playback",
            headers={
                **cookie,
                "Origin": settings.public_origin,
                "X-CSRF-Token": csrf,
            },
        )
        assert playback_response.status == 200
        assert (await playback_response.json())["autoplay_enabled"] is False
    finally:
        await client.close()
        await database.close()


async def test_playback_rejects_cookie_only_get_and_post_without_csrf(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    raw_session, _csrf = await database.create_web_session(
        123,
        {
            "catalogue": catalogue(),
            "episode": 3,
            "start_position": 0,
        },
        ttl_seconds=600,
    )
    media = MediaGateway(settings, database)
    core = FakeCore()
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, core, media).register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        cookie = {"Cookie": f"{settings.cookie_name}={raw_session}"}
        get_response = await client.get("/api/playback", headers=cookie)
        assert get_response.status == 405

        post_response = await client.post(
            "/api/playback",
            headers={
                **cookie,
                "Origin": "https://evil.example",
            },
        )
        assert post_response.status == 403
        assert core.calls == 0
    finally:
        await client.close()
        await database.close()


async def test_playback_rate_limit_rejects_before_repeated_provider_work(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    raw_session, csrf = await database.create_web_session(
        123,
        {
            "catalogue": catalogue(),
            "episode": 3,
            "start_position": 0,
        },
        ttl_seconds=600,
    )
    media = MediaGateway(settings, database)
    core = FakeCore()
    routes = WebRoutes(settings, database, core, media)
    routes.playback_limiter = SlidingWindowLimiter(1, 60)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    routes.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {
            "Cookie": f"{settings.cookie_name}={raw_session}",
            "Origin": settings.public_origin,
            "X-CSRF-Token": csrf,
        }
        first = await client.post("/api/playback", headers=headers)
        assert first.status == 200

        limited = await client.post("/api/playback", headers=headers)
        assert limited.status == 429
        assert core.calls == 1
    finally:
        await client.close()
        await database.close()


async def test_episode_picker_rewind_becomes_the_resume_point(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    await database.record_progress(123, catalogue(), 3, 750.0, 1500, False)
    raw_session, csrf = await database.create_web_session(
        123,
        {
            "catalogue": catalogue(),
            "episode": 3,
            "start_position": 750.0,
        },
        ttl_seconds=600,
    )
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, FakeCore(), media).register(app)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        selected = await client.post(
            "/api/playback/episode",
            headers={
                "Origin": settings.public_origin,
                "X-CSRF-Token": csrf,
                "Cookie": f"{settings.cookie_name}={raw_session}",
            },
            json={"episode": 2},
        )
        assert selected.status == 200
        selected_payload = await selected.json()
        assert selected_payload["episode"] == 2
        assert selected_payload["start_position"] == 0.0

        saved = await client.post(
            "/api/progress",
            headers={
                "Origin": settings.public_origin,
                "X-CSRF-Token": csrf,
                "Cookie": f"{settings.cookie_name}={raw_session}",
            },
            json={
                "playback_id": selected_payload["playback_id"],
                "position": 300.0,
                "duration": 600.0,
                "completed": False,
            },
        )
        assert saved.status == 200

        resumed = (await database.continue_watching(123))[0]
        assert resumed["next_episode"] == 3
        assert resumed["last_played_episode"] == 2
        assert resumed["resume_episode"] == 2
        assert resumed["position"] == 300.0
        assert await database.episode_position(123, catalogue(), 3) == 750.0
    finally:
        await client.close()
        await database.close()


async def test_cast_endpoint_returns_short_lived_playback_grant(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(settings.allowed_users)
    raw_session, csrf = await database.create_web_session(
        123,
        {"catalogue": catalogue(), "episode": 3},
        ttl_seconds=600,
    )
    playback = await database.create_playback(
        123,
        catalogue(),
        3,
        media_url="https://cdn.example/episode-3.mp4",
        media_headers={},
        media_kind="mp4",
        source_name="Test",
        ttl_seconds=600,
    )
    media = MediaGateway(settings, database)
    app = web.Application(middlewares=[error_boundary, security_headers])
    app[CONFIG_KEY] = settings
    WebRoutes(settings, database, FakeCore(), media).register(app)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/cast",
            headers={
                "Origin": settings.public_origin,
                "X-CSRF-Token": csrf,
                "Cookie": f"{settings.cookie_name}={raw_session}",
            },
            json={"playback_id": playback.id},
        )
        assert response.status == 200
        payload = await response.json()
        assert payload["content_type"] == "video/mp4"
        assert payload["url"].startswith(
            f"{settings.public_base_url}/media/{playback.id}/master?cast="
        )
        grant = payload["url"].split("cast=", 1)[1]
        assert await database.get_cast_playback(grant, playback.id) is not None
    finally:
        await client.close()
        await database.close()
