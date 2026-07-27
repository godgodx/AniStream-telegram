from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from anistream.errors import ResolverError
from anistream.models import ResolvedMedia
from anistream.resolvers.base import Resolver, hostname
from anistream.resolvers.hosts import JwPlayerResolver
from anistream.utils.http import HttpClient


class FrembedRedirectResolver(Resolver):
    """Resolve the narrow Frembed redirect routes selected by CineHD."""

    name = "Reader gateway"
    _servers = {
        "link",
        "link4",
        "link7",
        "link4vo",
        "link7vo",
        "link4vostfr",
        "link7vostfr",
    }
    _uqload_hosts = ("uqload.is", "uqload.io", "uqload.com")

    def __init__(self, http: HttpClient) -> None:
        super().__init__(http)

    def matches(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or hostname(url) != "frembed.asia"
            or port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.path != "/api/stream"
            or parsed.fragment
        ):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        media_type = query.get("type", [])
        media_id = query.get("tmdb", [])
        server = query.get("server", [])
        if (
            len(media_type) != 1
            or len(media_id) != 1
            or len(server) != 1
            or media_type[0] not in {"movie", "serie"}
            or not media_id[0].isdigit()
            or not 1 <= int(media_id[0]) <= 2_147_483_647
            or server[0] not in self._servers
        ):
            return False

        required = {"type", "tmdb", "server"}
        if media_type[0] == "serie":
            required.update({"sa", "epi"})
            for key, minimum, maximum in (
                ("sa", 0, 999),
                ("epi", 1, 2_000),
            ):
                values = query.get(key, [])
                if (
                    len(values) != 1
                    or not values[0].isdigit()
                    or not minimum <= int(values[0]) <= maximum
                ):
                    return False
        return set(query) == required

    def resolve(self, url: str) -> ResolvedMedia:
        if not self.matches(url):
            raise ResolverError("reader URL is malformed or unsupported")
        response = self.http.get(
            url,
            headers={"Referer": "https://frembed.asia/"},
            allow_redirects=False,
        )
        if response.status_code == 404:
            raise ResolverError("reader is unavailable for this episode")
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise ResolverError(f"reader returned HTTP {response.status_code}")
        location = response.headers.get("Location", "").strip()
        target = urljoin(url, location)
        if not location:
            raise ResolverError("reader redirected without a destination")
        if not self._allowed_uqload(target):
            target = self._uqload_from_multiplexer(target)
            if target is None:
                raise ResolverError("reader redirected to an unsupported host")

        direct = parse_qs(urlparse(target).query).get("url", [""])[0]
        if direct and re.search(r"\.(?:m3u8|mp4)(?:$|\?)", direct, re.IGNORECASE):
            try:
                direct_parts = urlparse(direct)
                direct_port = direct_parts.port or (
                    443 if direct_parts.scheme == "https" else 80
                )
            except ValueError as exc:
                raise ResolverError("reader returned a malformed media URL") from exc
            direct_host = (direct_parts.hostname or "").casefold().rstrip(".")
            if (
                direct_parts.scheme not in {"http", "https"}
                or direct_host not in {"streamtales.cc"}
                or direct_port not in {80, 443}
                or direct_parts.username
                or direct_parts.password
            ):
                raise ResolverError("reader returned an unsupported media URL")
            return ResolvedMedia(
                direct,
                url,
                self.name,
                self.media_headers(target),
                "hls" if ".m3u8" in urlparse(direct).path.casefold() else "mp4",
            )

        host = hostname(target)
        base_host = next(
            (
                allowed
                for allowed in self._uqload_hosts
                if host == allowed or host.endswith("." + allowed)
            ),
            None,
        )
        if base_host is None:
            raise ResolverError("reader returned an unsupported Uqload host")
        nested = JwPlayerResolver(self.http, base_host).resolve(target)
        return ResolvedMedia(
            nested.url,
            url,
            self.name,
            nested.headers,
            nested.kind,
        )

    @classmethod
    def _allowed_uqload(cls, url: str) -> bool:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold().rstrip(".")
        return (
            parsed.scheme in {"http", "https"}
            and port
            in (
                None,
                443 if parsed.scheme == "https" else 80,
            )
            and not parsed.username
            and not parsed.password
            and any(
                host == allowed or host.endswith("." + allowed)
                for allowed in cls._uqload_hosts
            )
        )

    @classmethod
    def _uqload_from_multiplexer(cls, url: str) -> str | None:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") != "frembed.fun"
            or port not in (None, 443)
            or parsed.path != "/player/vidplayer.php"
            or parsed.username
            or parsed.password
        ):
            return None

        values = parse_qs(parsed.query, keep_blank_values=False).get("url", [])
        if len(values) != 1:
            return None
        for candidate in values[0].split(","):
            candidate = candidate.strip()
            if cls._allowed_uqload(candidate):
                return candidate
        return None
