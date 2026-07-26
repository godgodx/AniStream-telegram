from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

from cryptography.fernet import Fernet, InvalidToken


class AuthenticationError(ValueError):
    pass


class UnsafeUpstreamError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    user_id: int
    auth_date: int
    first_name: str
    username: str


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int,
    now: int | None = None,
) -> TelegramIdentity:
    if not init_data or len(init_data) > 8192:
        raise AuthenticationError("missing or oversized Telegram authentication data")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AuthenticationError("duplicate Telegram authentication fields")
    values = dict(pairs)
    supplied_hash = values.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied_hash):
        raise AuthenticationError("invalid Telegram authentication hash")
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.casefold()):
        raise AuthenticationError("Telegram authentication signature mismatch")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Telegram authentication payload is malformed") from exc
    timestamp = int(time.time()) if now is None else now
    if auth_date > timestamp + 30 or timestamp - auth_date > max_age_seconds:
        raise AuthenticationError("Telegram authentication data is stale")
    if user_id <= 0:
        raise AuthenticationError("Telegram user ID is invalid")
    return TelegramIdentity(
        user_id=user_id,
        auth_date=auth_date,
        first_name=str(user.get("first_name", ""))[:128],
        username=str(user.get("username", ""))[:64],
    )


def sanitize_upstream_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language"}
    clean: dict[str, str] = {}
    for name, value in headers.items():
        if name.casefold() not in allowed:
            continue
        text = str(value)
        if "\r" in text or "\n" in text or len(text) > 2048:
            continue
        clean[name] = text
    return clean


def public_url_parts(url: str, allowed_hosts: frozenset[str] = frozenset()) -> tuple[str, int]:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUpstreamError("malformed upstream URL") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise UnsafeUpstreamError("only absolute HTTP(S) upstream URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUpstreamError("upstream credentials are forbidden")
    if port not in {80, 443}:
        raise UnsafeUpstreamError("upstream port is forbidden")
    if allowed_hosts and not any(
        host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts
    ):
        raise UnsafeUpstreamError("upstream host is not in MEDIA_ALLOWED_HOSTS")
    return host, port


def validate_public_addresses(host: str, addresses: list[str]) -> None:
    if not addresses:
        raise UnsafeUpstreamError(f"DNS returned no addresses for {host}")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeUpstreamError(f"DNS returned an invalid address for {host}") from exc
        if not address.is_global:
            raise UnsafeUpstreamError(f"{host} resolved to a non-public network")


def resolve_public_host(host: str, port: int) -> list[str]:
    try:
        addresses = list(
            dict.fromkeys(
                item[4][0]
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        )
    except socket.gaierror as exc:
        raise UnsafeUpstreamError(f"DNS resolution failed for {host}") from exc
    validate_public_addresses(host, addresses)
    return addresses


class OpaqueMediaToken:
    def __init__(self, secret: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def create(self, playback_id: str, url: str, expires_at: int) -> str:
        payload = json.dumps(
            {"p": playback_id, "u": url, "e": expires_at},
            separators=(",", ":"),
        ).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def parse(self, token: str, playback_id: str, *, now: int | None = None) -> str:
        if len(token) > 8192:
            raise AuthenticationError("media token is oversized")
        try:
            payload = json.loads(self._fernet.decrypt(token.encode("ascii"), ttl=None))
            expires_at = int(payload["e"])
            url = str(payload["u"])
        except (InvalidToken, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise AuthenticationError("media token is invalid") from exc
        if not hmac.compare_digest(str(payload.get("p", "")), playback_id):
            raise AuthenticationError("media token belongs to another playback")
        timestamp = int(time.time()) if now is None else now
        if expires_at <= timestamp:
            raise AuthenticationError("media token expired")
        return url
