from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from anistream_telegram.config import Config
from anistream_telegram.database import Database
from anistream_telegram.media import MediaGateway
from anistream_telegram.web import (
    CONFIG_KEY,
    WebRoutes,
    error_boundary,
    security_headers,
)


BOT_TOKEN = "123456789:test-token"


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
