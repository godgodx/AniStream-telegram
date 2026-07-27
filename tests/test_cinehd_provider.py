from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from anistream.errors import ProviderError, ResolverError
from anistream.providers.cinehd import CineHdProvider
from anistream.resolvers.frembed import FrembedRedirectResolver


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "",
        payload: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self.text = text
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            return json.loads(self.text)
        return self._payload


class ProviderHttp:
    user_agent = "test-agent"

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        parsed = urlparse(url)
        params = kwargs.get("params")
        query = {
            key: str(value)
            for key, value in (params.items() if isinstance(params, dict) else [])
        }
        if parsed.path == "/api/search/discover":
            return FakeResponse(
                payload={
                    "results": [
                        {
                            "id": 100,
                            "media_type": "tv",
                            "name": "Example Series",
                        },
                        {
                            "id": 200,
                            "media_type": "movie",
                            "title": "Example Movie",
                        },
                        {"id": 300, "media_type": "person", "name": "Ignored"},
                    ]
                }
            )
        if parsed.path == "/tv/100":
            details = {
                "id": 100,
                "name": "Example Series",
                "seasons": [
                    {"season_number": 1, "name": "Season 1", "episode_count": 3},
                    {"season_number": 2, "name": "Season 2", "episode_count": 2},
                ],
            }
            inner = f'2b:["$","div",null,{{"props":{json.dumps(details)}}}]'
            pushed = json.dumps([1, inner])
            return FakeResponse(
                text=f"<html><h1>Example Series</h1><script>"
                f"self.__next_f.push({pushed})"
                "</script></html>"
            )
        if parsed.path == "/api/series":
            season = int(query["sa"])
            payload = {"link7": "/api/stream?server=link7"}
            if season == 1:
                payload["link7vo"] = "/api/stream?server=link7vo"
            return FakeResponse(payload=payload)
        if parsed.path == "/api/films":
            return FakeResponse(
                payload={
                    "link7": "/api/stream?server=link7",
                    "link7vo": "/api/stream?server=link7vo",
                }
            )
        raise AssertionError(f"unexpected GET: {url} {query}")


class ResolverHttp:
    user_agent = "test-agent"

    def __init__(self, location: str) -> None:
        self.location = location

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        assert kwargs.get("allow_redirects") is False
        return FakeResponse(status=302, headers={"Location": self.location})


def test_cinehd_matches_only_exact_catalogue_routes() -> None:
    provider = CineHdProvider(ProviderHttp())  # type: ignore[arg-type]
    assert provider.matches("https://cinehd.app/movie/123")
    assert provider.matches("cinehd.app/tv/456/")
    assert provider.matches("https://www.cinehd.app/tv/456")
    assert not provider.matches("https://cinehd.app/home")
    assert not provider.matches("https://cinehd.app.evil.example/tv/456")
    assert not provider.matches("https://evilcinehd.app/movie/123")


def test_cinehd_search_variants_and_catalogue_are_provider_neutral() -> None:
    provider = CineHdProvider(ProviderHttp())  # type: ignore[arg-type]
    results = provider.search("example")
    assert [(item.title, item.url) for item in results] == [
        ("Example Series", "https://cinehd.app/tv/100"),
        ("Example Movie", "https://cinehd.app/movie/200"),
    ]

    variants = provider.variants(results[0].url)
    assert [(item.season, item.language.code) for item in variants] == [
        ("Season 1", "fr-fr"),
        ("Season 1", "en-us"),
        ("Season 2", "fr-fr"),
    ]
    selected = variants[1]
    catalogue = provider.catalogue(selected.url)
    assert catalogue.title == "Example Series"
    assert catalogue.season == "Season 1"
    assert catalogue.language == selected.language
    assert [item.number for item in catalogue.episodes] == [1, 2, 3]
    assert len(catalogue.episodes[0].candidates) == 4
    first_query = parse_qs(urlparse(catalogue.episodes[0].candidates[0].url).query)
    assert first_query == {
        "type": ["serie"],
        "tmdb": ["100"],
        "server": ["link7vo"],
        "sa": ["1"],
        "epi": ["1"],
    }

    movie_variants = provider.variants(results[1].url)
    assert [item.language.code for item in movie_variants] == ["fr-fr", "en-us"]
    movie = provider.catalogue(movie_variants[0].url)
    assert movie.season == "Movie"
    assert len(movie.episodes) == 1
    assert len(movie.episodes[0].candidates) == 3


def test_cinehd_rejects_unavailable_language_deep_link() -> None:
    provider = CineHdProvider(ProviderHttp())  # type: ignore[arg-type]
    provider.search("example")
    with pytest.raises(ProviderError, match="no longer has compatible readers"):
        provider.catalogue(
            "https://cinehd.app/tv/100"
            "?anistream_lang=en-us&anistream_season=2"
        )


def test_frembed_redirect_resolver_accepts_only_generated_reader_routes() -> None:
    url = (
        "https://frembed.asia/api/stream"
        "?type=movie&tmdb=200&server=link7vo"
    )
    resolver = FrembedRedirectResolver(
        ResolverHttp(
            "https://uqload.is/player/frvod.php"
            "?url=https://streamtales.cc/video.mp4"
        )  # type: ignore[arg-type]
    )
    assert resolver.matches(url)
    assert not resolver.matches(
        "https://frembed.asia.evil.example/api/stream"
        "?type=movie&tmdb=200&server=link7vo"
    )
    assert not resolver.matches(
        "https://frembed.asia/api/stream"
        "?type=movie&tmdb=200&server=attacker"
    )
    assert not resolver.matches(
        "https://frembed.asia:8443/api/stream"
        "?type=movie&tmdb=200&server=link7vo"
    )
    assert not resolver.matches(
        "https://frembed.asia/api/stream"
        "?type=serie&tmdb=200&server=link"
    )
    assert not resolver.matches(
        "https://frembed.asia/api/stream"
        "?type=movie&tmdb=200&server=link7vo&next=https://evil.example"
    )
    media = resolver.resolve(url)
    assert media.url == "https://streamtales.cc/video.mp4"
    assert media.kind == "mp4"
    assert media.embed_url == url


def test_frembed_redirect_resolver_rejects_untrusted_redirects() -> None:
    resolver = FrembedRedirectResolver(
        ResolverHttp("https://evil.example/video.mp4")  # type: ignore[arg-type]
    )
    with pytest.raises(ResolverError, match="unsupported host"):
        resolver.resolve(
            "https://frembed.asia/api/stream"
            "?type=movie&tmdb=200&server=link7"
        )


def test_frembed_redirect_resolver_extracts_only_uqload_from_known_multiplexer() -> None:
    url = (
        "https://frembed.asia/api/stream"
        "?type=serie&tmdb=100&server=link&sa=1&epi=1"
    )
    resolver = FrembedRedirectResolver(
        ResolverHttp(
            "https://frembed.fun/player/vidplayer.php"
            "?url=https://vido.lol/embed/example,"
            "https://uqload.io/player/frvod.php"
            "?url=https%3A%2F%2Fstreamtales.cc%2Fvideo.mp4"
        )  # type: ignore[arg-type]
    )
    media = resolver.resolve(url)
    assert media.url == "https://streamtales.cc/video.mp4"
    assert media.kind == "mp4"
