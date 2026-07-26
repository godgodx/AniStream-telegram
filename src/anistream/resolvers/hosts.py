from __future__ import annotations

import binascii
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from anistream.errors import ResolverError
from anistream.models import ResolvedMedia
from anistream.resolvers.base import Resolver, hostname
from anistream.utils.http import HttpClient
from anistream.utils.js_unpack import unpack_packer


def _kind(url: str) -> str:
    path = urlparse(url).path.lower()
    if ".m3u8" in path:
        return "hls"
    if ".mp4" in path:
        return "mp4"
    return "unknown"


def _extract_media_url(content: str) -> str | None:
    patterns = (
        r"(?:file|source)\s*:\s*['\"](https?://[^'\"]+)['\"]",
        r"sources\s*:\s*\[\s*['\"](https?://[^'\"]+)['\"]",
        r"<source[^>]+src=['\"](https?://[^'\"]+)['\"]",
        r"(https?://[^'\"\\\s]+\.(?:m3u8|mp4)(?:\?[^'\"\\\s]*)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1).replace("\\/", "/")
    return None


class DirectMediaResolver(Resolver):
    name = "Direct media"

    def matches(self, url: str) -> bool:
        return bool(re.search(r"\.(m3u8|mp4)(?:$|\?)", url, re.IGNORECASE))

    def resolve(self, url: str) -> ResolvedMedia:
        return ResolvedMedia(url, url, self.name, self.media_headers(url), _kind(url))


class SendvidResolver(Resolver):
    name = "Sendvid"

    def matches(self, url: str) -> bool:
        return hostname(url).endswith("sendvid.com")

    def resolve(self, url: str) -> ResolvedMedia:
        response = self.http.get(url)
        if response.status_code != 200:
            raise ResolverError(f"Sendvid returned HTTP {response.status_code}")
        media_url = _extract_media_url(response.text)
        if not media_url:
            match = re.search(r"var\s+video_source\s*=\s*['\"]([^'\"]+)", response.text)
            media_url = match.group(1) if match else None
        if not media_url:
            raise ResolverError("Sendvid did not expose a media source")
        return ResolvedMedia(media_url, url, self.name, self.media_headers(url), _kind(media_url))


class SibnetResolver(Resolver):
    name = "Sibnet"

    def matches(self, url: str) -> bool:
        return hostname(url).endswith("sibnet.ru")

    def resolve(self, url: str) -> ResolvedMedia:
        response = self.http.get(url)
        if response.status_code != 200:
            raise ResolverError(f"Sibnet returned HTTP {response.status_code}")
        match = re.search(r"player\.src\s*\(\s*\[\s*\{.*?src\s*:\s*['\"]([^'\"]+)", response.text, re.DOTALL)
        if not match:
            raise ResolverError("Sibnet did not expose a player source")
        redirect = urljoin("https://video.sibnet.ru/", match.group(1))
        headers = self.media_headers(url, "https://video.sibnet.ru/")
        media_response = self.http.get(redirect, headers=headers, allow_redirects=False)
        if media_response.status_code in (301, 302, 303, 307, 308):
            media_url = urljoin(redirect, media_response.headers.get("Location", ""))
        elif media_response.status_code == 200:
            media_url = media_response.url
        else:
            raise ResolverError(f"Sibnet media redirect returned HTTP {media_response.status_code}")
        if not media_url:
            raise ResolverError("Sibnet returned an empty media redirect")
        return ResolvedMedia(media_url, url, self.name, headers, _kind(media_url))


class Embed4MeResolver(Resolver):
    name = "Embed4me"
    _key = b"kiemtienmua911ca"
    _iv = b"1234567890oiuytr"

    def matches(self, url: str) -> bool:
        return "embed4me" in hostname(url)

    def resolve(self, url: str) -> ResolvedMedia:
        match = re.search(r"#([a-zA-Z0-9]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9]+)", url)
        if not match:
            raise ResolverError("Embed4me URL is missing a video ID")
        video_id = match.group(1)
        api_url = (
            "https://lpayer.embed4me.com/api/v1/video"
            f"?id={video_id}&w=1920&h=1080&r=https://lpayer.embed4me.com/"
        )
        headers = self.media_headers(url, "https://lpayer.embed4me.com/")
        last_error = "empty response"
        for _ in range(3):
            try:
                response = self.http.get(api_url, headers=headers)
                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}"
                    continue
                encrypted = response.text.strip().strip('"')
                cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
                decoded = unpad(cipher.decrypt(binascii.unhexlify(encrypted)), AES.block_size).decode("utf-8")
                payload = json.loads(decoded)
                source = payload.get("source")
                if isinstance(source, list):
                    source = next((item.get("file") for item in source if isinstance(item, dict) and item.get("file")), None)
                if isinstance(source, str) and source:
                    return ResolvedMedia(source, url, self.name, headers, _kind(source))
                last_error = "decrypted payload did not contain a source"
            except (ValueError, binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
                last_error = str(exc)
        raise ResolverError(f"Embed4me resolution failed: {last_error}")


class VidmolyResolver(Resolver):
    name = "Vidmoly"

    def matches(self, url: str) -> bool:
        return "vidmoly" in hostname(url)

    def resolve(self, url: str) -> ResolvedMedia:
        candidates = [url]
        if hostname(url) in {"vidmoly.to", "vidmoly.net"}:
            candidates.append(re.sub(r"vidmoly\.(?:to|net)", "vidmoly.biz", url, count=1))
        last_error = "no response"
        for candidate in candidates:
            response = self.http.get(candidate, headers={"Referer": "https://vidmoly.to/"})
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                continue
            content = response.text
            media_url = _extract_media_url(content)
            if not media_url:
                unpacked = unpack_packer(content)
                media_url = _extract_media_url(unpacked or "")
            if not media_url:
                token = re.search(r"[?&]g=([a-f0-9]{32})", content, re.IGNORECASE)
                if token:
                    second = self.http.get(candidate + ("&" if "?" in candidate else "?") + "g=" + token.group(1))
                    media_url = _extract_media_url(second.text)
            if media_url:
                headers = self.media_headers(candidate, "https://vidmoly.to/")
                return ResolvedMedia(media_url, url, self.name, headers, _kind(media_url))
            last_error = "player script contained no media URL"
        raise ResolverError(f"Vidmoly resolution failed: {last_error}")


class VidzyResolver(Resolver):
    name = "Vidzy"

    def matches(self, url: str) -> bool:
        host = hostname(url)
        return host == "vidzy.cc" or host == "vidzy.org" or host.endswith((".vidzy.cc", ".vidzy.org"))

    def resolve(self, url: str) -> ResolvedMedia:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        response = self.http.get(url, headers={"Referer": origin})
        if response.status_code != 200:
            raise ResolverError(f"Vidzy returned HTTP {response.status_code}")
        media_url = _extract_media_url(response.text)
        if not media_url:
            media_url = _extract_media_url(unpack_packer(response.text) or "")
        if not media_url:
            raise ResolverError("Vidzy did not expose a media source")
        return ResolvedMedia(media_url, url, self.name, self.media_headers(url), _kind(media_url))


class JwPlayerResolver(Resolver):
    host_names = (
        "oneupload.net",
        "oneupload.to",
        "uqload.is",
        "uqload.com",
        "smoothpre.com",
        "movearnpre.com",
        "mivalyo.com",
        "dingtezuni.com",
    )

    def __init__(self, http: HttpClient, host_name: str) -> None:
        super().__init__(http)
        self.host_name = host_name
        self.name = host_name.split(".")[0].title()

    def matches(self, url: str) -> bool:
        return hostname(url).endswith(self.host_name)

    def resolve(self, url: str) -> ResolvedMedia:
        response = self.http.get(url, headers={"Referer": f"https://{self.host_name}/"})
        if response.status_code != 200:
            raise ResolverError(f"{self.name} returned HTTP {response.status_code}")
        content = response.text
        media_url = _extract_media_url(content)
        if not media_url:
            unpacked = unpack_packer(content)
            media_url = _extract_media_url(unpacked or "")
        if not media_url:
            soup = BeautifulSoup(content, "html.parser")
            source = soup.find("source", src=True)
            media_url = str(source["src"]) if source else None
        if not media_url:
            raise ResolverError(f"{self.name} did not expose a media source")
        media_url = urljoin(url, media_url)
        headers = self.media_headers(url, f"https://{self.host_name}/")
        return ResolvedMedia(media_url, url, self.name, headers, _kind(media_url))


def default_resolvers(http: HttpClient) -> list[Resolver]:
    resolvers: list[Resolver] = [
        Embed4MeResolver(http),
        SendvidResolver(http),
        SibnetResolver(http),
        VidmolyResolver(http),
        VidzyResolver(http),
    ]
    resolvers.extend(JwPlayerResolver(http, host) for host in JwPlayerResolver.host_names)
    resolvers.append(DirectMediaResolver(http))
    return resolvers
