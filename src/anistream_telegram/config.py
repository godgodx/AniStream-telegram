from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _users(value: str) -> tuple[int, ...]:
    users: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise ValueError("TELEGRAM_ALLOWED_USERS must contain numeric Telegram IDs") from exc
        if user_id <= 0:
            raise ValueError("Telegram IDs must be positive")
        users.append(user_id)
    return tuple(dict.fromkeys(users))


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    allowed_users: tuple[int, ...]
    public_base_url: str
    webhook_secret: str
    session_secret: str
    database_url: str
    run_mode: str
    cookie_secure: bool
    trusted_proxy_count: int
    auth_max_age_seconds: int
    session_ttl_seconds: int
    playback_ttl_seconds: int
    max_streams_per_user: int
    anime_sama_user_agent: str
    anime_sama_cf_clearance: str
    media_allowed_hosts: frozenset[str]
    log_level: str
    project_root: Path

    @property
    def webhook_path(self) -> str:
        return "/telegram/webhook"

    @property
    def webhook_url(self) -> str:
        return self.public_base_url + self.webhook_path

    @property
    def cookie_name(self) -> str:
        return "__Host-anistream_session" if self.cookie_secure else "anistream_session"

    @property
    def public_origin(self) -> str:
        parsed = urlparse(self.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @classmethod
    def from_env(cls, *, require_secrets: bool = True) -> "Config":
        root = Path(os.getenv("ANISTREAM_ROOT", os.getcwd())).resolve()
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        base_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/")
        webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
        session_secret = os.getenv("SESSION_SECRET", "").strip()
        run_mode = os.getenv("RUN_MODE", "webhook").strip().casefold()
        parsed = urlparse(base_url)

        if run_mode not in {"webhook", "polling"}:
            raise ValueError("RUN_MODE must be webhook or polling")
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("PUBLIC_BASE_URL must be an absolute URL")
        cookie_secure = _bool("COOKIE_SECURE", True)
        if require_secrets:
            if ":" not in bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN is missing or malformed")
            if len(webhook_secret) < 24:
                raise ValueError("WEBHOOK_SECRET must contain at least 24 characters")
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", webhook_secret):
                raise ValueError(
                    "WEBHOOK_SECRET may contain only letters, digits, underscore and hyphen"
                )
            if len(session_secret.encode("utf-8")) < 32:
                raise ValueError("SESSION_SECRET must contain at least 32 bytes")
            if run_mode == "webhook" and parsed.scheme != "https":
                raise ValueError("Webhook mode requires an HTTPS PUBLIC_BASE_URL")
            if cookie_secure and parsed.scheme != "https":
                raise ValueError("COOKIE_SECURE=true requires HTTPS")

        allowed_hosts = frozenset(
            host.strip().casefold().rstrip(".")
            for host in os.getenv("MEDIA_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        session_ttl = _int("SESSION_TTL_SECONDS", 21600, 300, 86400)
        playback_ttl = _int("PLAYBACK_TTL_SECONDS", 21600, 300, 43200)
        if playback_ttl > session_ttl:
            raise ValueError("PLAYBACK_TTL_SECONDS cannot exceed SESSION_TTL_SECONDS")
        return cls(
            bot_token=bot_token,
            allowed_users=_users(os.getenv("TELEGRAM_ALLOWED_USERS", "")),
            public_base_url=base_url,
            webhook_secret=webhook_secret,
            session_secret=session_secret,
            database_url=os.getenv(
                "DATABASE_URL",
                f"sqlite+aiosqlite:///{(root / 'data' / 'anistream.db').as_posix()}",
            ),
            run_mode=run_mode,
            cookie_secure=cookie_secure,
            trusted_proxy_count=_int("TRUSTED_PROXY_COUNT", 1, 0, 5),
            auth_max_age_seconds=_int("AUTH_MAX_AGE_SECONDS", 300, 30, 1800),
            session_ttl_seconds=session_ttl,
            playback_ttl_seconds=playback_ttl,
            max_streams_per_user=_int("MAX_STREAMS_PER_USER", 2, 1, 5),
            anime_sama_user_agent=os.getenv("ANIME_SAMA_USER_AGENT", "").strip(),
            anime_sama_cf_clearance=os.getenv("ANIME_SAMA_CF_CLEARANCE", "").strip(),
            media_allowed_hosts=allowed_hosts,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            project_root=root,
        )
