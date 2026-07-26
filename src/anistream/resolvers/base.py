from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import urlparse

from anistream.models import ResolvedMedia
from anistream.utils.http import HttpClient


def hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "unknown").lower()
    except ValueError:
        return "unknown"


class Resolver(ABC):
    name: str

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @abstractmethod
    def matches(self, url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def resolve(self, url: str) -> ResolvedMedia:
        raise NotImplementedError

    def media_headers(self, embed_url: str, referer: str | None = None) -> dict[str, str]:
        parsed = urlparse(referer or embed_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return {
            "User-Agent": self.http.user_agent,
            "Referer": referer or origin + "/",
            "Origin": origin,
        }
