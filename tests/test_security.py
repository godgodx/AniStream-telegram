from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from anistream_telegram.security import (
    AuthenticationError,
    OpaqueMediaToken,
    UnsafeUpstreamError,
    public_url_parts,
    validate_public_addresses,
    validate_telegram_init_data,
)


BOT_TOKEN = "123456789:test-token"


def init_data(user_id: int, auth_date: int) -> str:
    values = {
        "auth_date": str(auth_date),
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


def test_validates_telegram_identity() -> None:
    value = init_data(9007199254740, 1_700_000_000)
    identity = validate_telegram_init_data(
        value,
        BOT_TOKEN,
        max_age_seconds=300,
        now=1_700_000_100,
    )
    assert identity.user_id == 9007199254740
    assert identity.first_name == "Test"


def test_rejects_forged_or_stale_init_data() -> None:
    value = init_data(42, 1_700_000_000)
    with pytest.raises(AuthenticationError):
        validate_telegram_init_data(
            value.replace("Test", "Admin"),
            BOT_TOKEN,
            max_age_seconds=300,
            now=1_700_000_100,
        )
    with pytest.raises(AuthenticationError):
        validate_telegram_init_data(
            value,
            BOT_TOKEN,
            max_age_seconds=30,
            now=1_700_000_100,
        )


def test_rejects_duplicate_authentication_fields() -> None:
    value = init_data(42, 1_700_000_000) + "&auth_date=1700000000"
    with pytest.raises(AuthenticationError):
        validate_telegram_init_data(
            value,
            BOT_TOKEN,
            max_age_seconds=300,
            now=1_700_000_100,
        )


def test_media_token_is_bound_to_playback_and_expiry() -> None:
    tokens = OpaqueMediaToken("x" * 32)
    token = tokens.create("playback-a", "https://cdn.example/video.m3u8", 2000)
    assert (
        tokens.parse(token, "playback-a", now=1900)
        == "https://cdn.example/video.m3u8"
    )
    with pytest.raises(AuthenticationError):
        tokens.parse(token, "playback-b", now=1900)
    with pytest.raises(AuthenticationError):
        tokens.parse(token, "playback-a", now=2000)


def test_upstream_policy_blocks_credentials_ports_and_private_addresses() -> None:
    assert public_url_parts("https://cdn.example/video.mp4") == ("cdn.example", 443)
    with pytest.raises(UnsafeUpstreamError):
        public_url_parts("https://user:pass@cdn.example/video.mp4")
    with pytest.raises(UnsafeUpstreamError):
        public_url_parts("https://cdn.example:8443/video.mp4")
    with pytest.raises(UnsafeUpstreamError):
        validate_public_addresses("localhost", ["127.0.0.1"])
    with pytest.raises(UnsafeUpstreamError):
        validate_public_addresses("metadata", ["169.254.169.254"])


def test_optional_media_host_allowlist_is_exact_or_subdomain() -> None:
    allowed = frozenset({"media.example"})
    assert public_url_parts("https://cdn.media.example/a", allowed)[0] == "cdn.media.example"
    with pytest.raises(UnsafeUpstreamError):
        public_url_parts("https://media.example.evil.test/a", allowed)
