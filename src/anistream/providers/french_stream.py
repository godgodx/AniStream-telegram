from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from anistream.errors import ProviderError
from anistream.models import Catalogue, CatalogueVariant, EmbedCandidate, Episode, MediaLanguage, SearchResult
from anistream.providers.base import Provider
from anistream.utils.http import HttpClient


@dataclass(frozen=True, slots=True)
class _Page:
    url: str
    news_id: str
    title: str
    season: str
    kind: str
    version: str = ""
    series_tag: str = ""


class FrenchStreamProvider(Provider):
    id = "french_stream"
    name = "French Stream"
    base_url = "https://french-stream.one/"
    search_url = "https://french-stream.one/engine/ajax/search.php"

    _language_parameter = "anistream_lang"
    _language_labels = {
        "vf": "French (VF)",
        "vostfr": "VOSTFR",
        "vo": "VO / VOSTENG",
        "vfq": "French Canadian (VFQ)",
        "vff": "TrueFrench (VFF)",
    }
    _player_order = ("vidzy", "uqload", "voe", "netu", "dood", "filmoon")

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._pages: dict[str, _Page] = {}
        self._series_payloads: dict[str, dict] = {}
        self._film_payloads: dict[str, dict] = {}

    def matches(self, url: str) -> bool:
        try:
            host = (urlparse(self._with_scheme(url)).hostname or "").lower()
        except ValueError:
            return False
        return host in {"french-stream.one", "www.french-stream.one"}

    def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []
        response = self.http.post(
            self.search_url,
            data={"query": query, "page": 1},
            headers={"Referer": self.base_url},
        )
        if response.status_code != 200:
            raise ProviderError(f"search returned HTTP {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in soup.select(".search-item[onclick]"):
            match = re.search(r"location\.href\s*=\s*(['\"])(.*?)\1", str(item.get("onclick", "")))
            title_node = item.select_one(".search-title")
            if not match or title_node is None:
                continue
            url = urljoin(self.base_url, match.group(2).strip())
            if url in seen or not self.matches(url) or not self._news_id(url):
                continue
            title = title_node.get_text(" ", strip=True)
            title = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
            if not title:
                continue
            seen.add(url)
            results.append(SearchResult(self.id, self.name, title, url))
        return results

    def variants(self, url: str) -> list[CatalogueVariant]:
        normalized = self._normalize_url(url)
        selected = self._selected_language(normalized)
        page = self._page(normalized)
        if selected:
            languages = self._languages(page)
            language = languages.get(selected)
            if language is None:
                raise ProviderError(f"language variant is not available: {selected}")
            return [self._variant(page, language)]

        pages = [page]
        if page.kind == "series":
            pages.extend(self._related_seasons(page))

        variants: list[CatalogueVariant] = []
        seen: set[tuple[str, str]] = set()
        for item in sorted(pages, key=lambda value: self._season_sort_key(value.season)):
            for language in self._languages(item).values():
                key = (item.url, language.code)
                if key in seen:
                    continue
                seen.add(key)
                variants.append(self._variant(item, language))
        if not variants:
            raise ProviderError("no playable season or language variants were found")
        return variants

    def catalogue(self, url: str) -> Catalogue:
        normalized = self._normalize_url(url)
        page = self._page(normalized)
        languages = self._languages(page)
        selected = self._selected_language(normalized)
        if not selected:
            if len(languages) != 1:
                raise ProviderError("select a season and language before loading episodes")
            selected = next(iter(languages))
        language = languages.get(selected)
        if language is None:
            raise ProviderError(f"language variant is not available: {selected}")

        if page.kind == "series":
            episodes = self._series_episodes(page, language.code)
        else:
            routes = self._film_routes(page).get(language.code, ())
            if not routes:
                raise ProviderError("the selected film language has no usable embed links")
            episodes = (Episode(1, routes),)

        return Catalogue(
            provider_id=self.id,
            provider_name=self.name,
            title=page.title,
            url=self._with_language(page.url, language.code),
            season=page.season,
            language=language,
            episodes=episodes,
        )

    def _page(self, url: str) -> _Page:
        native_url = self._native_url(url)
        cached = self._pages.get(native_url)
        if cached is not None:
            return cached
        response = self.http.get(native_url, headers={"Referer": self.base_url})
        if response.status_code != 200:
            raise ProviderError(f"catalogue page returned HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        news_id = self._news_id(native_url)
        if not news_id:
            raise ProviderError("the catalogue URL is missing a content ID")

        series = soup.select_one("#serie-data[data-newsid]")
        film = soup.select_one("#film-data[data-newsid]")
        if series is not None:
            raw_title = str(series.get("data-title") or "").strip()
            match = re.match(r"^(.*?)\s*-\s*Saison\s+(.+?)\s*$", raw_title, re.IGNORECASE)
            title = match.group(1).strip() if match else raw_title
            season = f"Season {match.group(2).strip()}" if match else "Series"
            tag_match = re.search(r"\bs-\d+\b", series.get_text(" ", strip=True), re.IGNORECASE)
            page = _Page(native_url, news_id, title, season, "series", series_tag=tag_match.group(0) if tag_match else "")
        elif film is not None:
            title = str(film.get("data-title") or "").strip()
            version_link = soup.select_one("a[href*='xfname=version-film']")
            version = version_link.get_text(" ", strip=True) if version_link else ""
            page = _Page(native_url, news_id, title, "Movie", "movie", version=version)
        else:
            raise ProviderError("the URL does not point to a French Stream film or series")
        if not page.title:
            raise ProviderError("the catalogue page did not expose a title")
        self._pages[native_url] = page
        return page

    def _related_seasons(self, page: _Page) -> list[_Page]:
        if not page.series_tag:
            return []
        response = self.http.get(
            urljoin(self.base_url, "engine/ajax/get_seasons.php"),
            params={"serie_tag": page.series_tag, "news_id": page.news_id},
            headers={"Referer": page.url},
        )
        if response.status_code != 200:
            return []
        data = self._json(response.text)
        if not isinstance(data, list):
            return []
        seasons: list[_Page] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("full_url"):
                continue
            related_url = urljoin(self.base_url, "/" + str(item["full_url"]).lstrip("/"))
            if self._news_id(related_url) == page.news_id:
                continue
            try:
                seasons.append(self._page(related_url))
            except ProviderError:
                continue
        return seasons

    def _languages(self, page: _Page) -> dict[str, MediaLanguage]:
        if page.kind == "series":
            payload = self._series_payload(page)
            codes = [
                code
                for code, episodes in payload.items()
                if code != "info" and isinstance(episodes, dict) and self._has_series_routes(episodes)
            ]
        else:
            codes = list(self._film_routes(page))
        return {code: self._language(code) for code in codes}

    def _series_payload(self, page: _Page) -> dict:
        cached = self._series_payloads.get(page.news_id)
        if cached is not None:
            return cached
        paths = (
            f"static/series/{page.news_id}.js",
            f"css/sr_{page.news_id}.css",
            f"font/sr_{page.news_id}.woff2",
            f"assets/poster_{page.news_id}.json",
            f"data/eps_{page.news_id}.txt",
            f"ep-data.php?id={page.news_id}&format=js",
        )
        for path in paths:
            response = self.http.get(urljoin(self.base_url, path), headers={"Referer": page.url})
            if response.status_code != 200:
                continue
            data = self._json(response.text)
            if isinstance(data, dict) and not data.get("error"):
                self._series_payloads[page.news_id] = data
                return data
        raise ProviderError("the series episode API returned no usable data")

    def _film_payload(self, page: _Page) -> dict:
        cached = self._film_payloads.get(page.news_id)
        if cached is not None:
            return cached
        response = self.http.get(
            urljoin(self.base_url, "engine/ajax/film_api.php"),
            params={"id": page.news_id},
            headers={"Referer": page.url},
        )
        if response.status_code != 200:
            raise ProviderError(f"film player API returned HTTP {response.status_code}")
        data = self._json(response.text)
        if not isinstance(data, dict) or not isinstance(data.get("players"), dict):
            raise ProviderError("the film player API returned malformed data")
        self._film_payloads[page.news_id] = data
        return data

    def _series_episodes(self, page: _Page, language: str) -> tuple[Episode, ...]:
        raw = self._series_payload(page).get(language)
        if not isinstance(raw, dict):
            raise ProviderError("the selected series language has no episodes")
        numbered = {int(key): value for key, value in raw.items() if str(key).isdigit() and isinstance(value, dict)}
        if not numbered:
            raise ProviderError("the selected series language has no episodes")
        numbers = sorted(numbered)
        if numbers != list(range(1, numbers[-1] + 1)):
            raise ProviderError("the series episode numbering is not contiguous")

        episodes: list[Episode] = []
        for number in numbers:
            candidates = self._candidates(numbered[number])
            if not candidates:
                raise ProviderError(f"episode {number} has no usable embed links")
            episodes.append(Episode(number, candidates))
        return tuple(episodes)

    def _film_routes(self, page: _Page) -> dict[str, tuple[EmbedCandidate, ...]]:
        players = self._film_payload(page).get("players", {})
        routes: dict[str, list[EmbedCandidate]] = {}
        seen: dict[str, set[str]] = {}
        default_code = self._default_film_language(page.version)
        ordered = self._player_order + tuple(name for name in players if name not in self._player_order and name != "premium")
        for player in ordered:
            versions = players.get(player)
            if not isinstance(versions, dict):
                continue
            for field, code in (("default", default_code), ("vostfr", "vostfr"), ("vfq", "vfq"), ("vff", "vff")):
                value = versions.get(field)
                if not self._valid_embed(value):
                    continue
                routes.setdefault(code, [])
                seen.setdefault(code, set())
                if value in seen[code]:
                    continue
                seen[code].add(value)
                routes[code].append(EmbedCandidate(self._player_label(player), value.strip()))
        return {code: tuple(candidates) for code, candidates in routes.items() if candidates}

    def _candidates(self, players: dict) -> tuple[EmbedCandidate, ...]:
        ordered = self._player_order + tuple(name for name in players if name not in self._player_order and name != "premium")
        candidates: list[EmbedCandidate] = []
        seen: set[str] = set()
        for player in ordered:
            value = players.get(player)
            if not self._valid_embed(value) or value in seen:
                continue
            seen.add(value)
            candidates.append(EmbedCandidate(self._player_label(player), value.strip()))
        return tuple(candidates)

    def _variant(self, page: _Page, language: MediaLanguage) -> CatalogueVariant:
        return CatalogueVariant(
            name=f"{page.season} - {language.label}",
            url=self._with_language(page.url, language.code),
            season=page.season,
            language=language,
        )

    def _normalize_url(self, url: str) -> str:
        value = self._with_scheme(url.strip())
        if not self.matches(value):
            raise ProviderError("this URL is not a French Stream URL")
        parsed = urlparse(value)
        if not self._news_id(value):
            raise ProviderError("the URL does not point to a French Stream film or series")
        query = [(key, item) for key, item in parse_qsl(parsed.query) if key in {"newsid", self._language_parameter}]
        return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", urlencode(query), ""))

    def _native_url(self, url: str) -> str:
        parsed = urlparse(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query) if key != self._language_parameter]
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))

    def _with_language(self, url: str, language: str) -> str:
        parsed = urlparse(self._native_url(url))
        query = parse_qsl(parsed.query)
        query.append((self._language_parameter, language))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))

    def _selected_language(self, url: str) -> str:
        return next((value.casefold() for key, value in parse_qsl(urlparse(url).query) if key == self._language_parameter), "")

    @staticmethod
    def _with_scheme(url: str) -> str:
        return url if url.startswith(("http://", "https://")) else "https://" + url

    @staticmethod
    def _news_id(url: str) -> str:
        parsed = urlparse(url)
        path_match = re.search(r"/(\d{2,})(?:-|\.html|$)", parsed.path)
        if path_match:
            return path_match.group(1)
        return next((value for key, value in parse_qsl(parsed.query) if key == "newsid" and value.isdigit()), "")

    @staticmethod
    def _json(value: str):
        try:
            return json.loads(value.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _valid_embed(value: object) -> bool:
        return isinstance(value, str) and value.strip().startswith(("http://", "https://")) and "[xfvalue_" not in value

    def _has_series_routes(self, episodes: dict) -> bool:
        return any(isinstance(players, dict) and bool(self._candidates(players)) for players in episodes.values())

    def _language(self, code: str) -> MediaLanguage:
        normalized = code.strip().casefold()
        return MediaLanguage(normalized, self._language_labels.get(normalized, normalized.upper()))

    @staticmethod
    def _default_film_language(version: str) -> str:
        normalized = re.sub(r"[^A-Z+]", "", version.upper())
        if normalized == "VOSTFR":
            return "vostfr"
        if "TRUEFRENCH" in normalized or normalized == "VFF":
            return "vff"
        if "VFQ" in normalized:
            return "vfq"
        return "vf"

    @staticmethod
    def _player_label(player: str) -> str:
        return {"uqload": "Uqload", "vidzy": "Vidzy", "voe": "VOE"}.get(player, player.replace("_", " ").title())

    @staticmethod
    def _season_sort_key(season: str) -> tuple[int, str]:
        match = re.search(r"(\d+)", season)
        return (int(match.group(1)) if match else 9999, season.casefold())
