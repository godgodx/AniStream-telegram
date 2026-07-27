from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from anistream.errors import ProviderError
from anistream.models import (
    Catalogue,
    CatalogueVariant,
    EmbedCandidate,
    Episode,
    MediaLanguage,
    SearchResult,
)
from anistream.providers.base import Provider
from anistream.utils.http import HttpClient


@dataclass(frozen=True, slots=True)
class _Title:
    url: str
    media_id: int
    kind: str
    title: str
    seasons: tuple[tuple[int, str, int], ...] = ()


class CineHdProvider(Provider):
    """CineHD catalogue backed by a small, verified FR/US reader subset."""

    id = "cinehd"
    name = "CineHD"
    base_url = "https://cinehd.app/"
    search_url = "https://cinehd.app/api/search/discover"
    reader_base_url = "https://frembed.asia/"
    reader_stream_url = "https://frembed.asia/api/stream"

    _language_parameter = "anistream_lang"
    _season_parameter = "anistream_season"
    _languages = {
        "fr-fr": MediaLanguage("fr-fr", "French (France)"),
        "en-us": MediaLanguage("en-us", "English (US)"),
    }
    _reader_fields = {
        "fr-fr": ("link7", "link", "link4"),
        # Prefer original-version readers, then retain VOSTFR as an English
        # audio fallback when that is the only compatible option.
        "en-us": ("link7vo", "link4vo", "link7vostfr", "link4vostfr"),
    }
    _next_push = "self.__next_f.push("

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._titles: dict[str, _Title] = {}
        self._reader_payloads: dict[tuple[str, int, int, int], dict] = {}

    def matches(self, url: str) -> bool:
        try:
            parsed = urlparse(self._with_scheme(url))
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold().rstrip(".")
        return host in {"cinehd.app", "www.cinehd.app"} and bool(
            re.fullmatch(r"/(?:movie|tv)/\d+/?", parsed.path)
        )

    def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []
        response = self.http.get(
            self.search_url,
            params={
                "type": "all",
                "page": 1,
                "sort": "popularity",
                "genre": "all",
                "year": "all",
                "watch_provider": "all",
                "country": "all",
                "rating": "all",
                "now_playing": "false",
                "trending": "false",
                "q": query,
            },
            headers={"Referer": self.base_url},
        )
        if response.status_code != 200:
            raise ProviderError(f"search returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("search returned malformed JSON") from exc
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            raise ProviderError("search returned malformed results")

        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in raw_results[:40]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("media_type", "")).casefold()
            try:
                media_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if kind not in {"movie", "tv"} or media_id <= 0 or not title:
                continue
            url = f"https://cinehd.app/{kind}/{media_id}"
            if url in seen:
                continue
            seen.add(url)
            self._titles[url] = _Title(url, media_id, kind, title)
            results.append(SearchResult(self.id, self.name, title, url))
        return results

    def variants(self, url: str) -> list[CatalogueVariant]:
        normalized = self._normalize_url(url)
        selected_language = self._selected(normalized, self._language_parameter)
        selected_season = self._selected_season(normalized)
        title = self._title(normalized)

        if selected_language:
            language = self._languages.get(selected_language)
            if language is None:
                raise ProviderError("the selected reader language is not supported")
            season_number = selected_season if title.kind == "tv" else 1
            if title.kind == "tv" and selected_season is None:
                raise ProviderError("select a season before loading this language")
            return [self._variant(title, season_number, language)]

        seasons = title.seasons if title.kind == "tv" else ((1, "Movie", 1),)
        checks: dict[tuple[int, str], bool] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(seasons)))) as pool:
            pending = {
                pool.submit(self._available_languages, title, season_number): (
                    season_number,
                    season_label,
                )
                for season_number, season_label, _ in seasons
            }
            for future in as_completed(pending):
                season_number, _ = pending[future]
                try:
                    for code in future.result():
                        checks[(season_number, code)] = True
                except ProviderError:
                    continue

        variants: list[CatalogueVariant] = []
        for season_number, _, _ in seasons:
            for code, language in self._languages.items():
                if checks.get((season_number, code)):
                    variants.append(self._variant(title, season_number, language))
        if not variants:
            raise ProviderError(
                "no compatible French or English (US) readers were found for this title"
            )
        return variants

    def catalogue(self, url: str) -> Catalogue:
        normalized = self._normalize_url(url)
        title = self._title(normalized)
        language_code = self._selected(normalized, self._language_parameter)
        language = self._languages.get(language_code)
        if language is None:
            raise ProviderError("select a supported language before loading episodes")

        if title.kind == "tv":
            season_number = self._selected_season(normalized)
            if season_number is None:
                raise ProviderError("select a season before loading episodes")
            season = next(
                (item for item in title.seasons if item[0] == season_number),
                None,
            )
            if season is None:
                raise ProviderError("the selected season is unavailable")
            _, season_label, episode_count = season
        else:
            season_number = 1
            season_label = "Movie"
            episode_count = 1

        if language_code not in self._available_languages(title, season_number):
            raise ProviderError("the selected language no longer has compatible readers")

        episodes = tuple(
            Episode(
                number,
                self._candidates(
                    title.kind,
                    title.media_id,
                    season_number,
                    number,
                    language_code,
                ),
            )
            for number in range(1, episode_count + 1)
        )
        return Catalogue(
            provider_id=self.id,
            provider_name=self.name,
            title=title.title,
            url=self._with_variant(title.url, season_number, language_code),
            season=season_label,
            language=language,
            episodes=episodes,
        )

    def _title(self, url: str) -> _Title:
        native_url = self._native_url(url)
        cached = self._titles.get(native_url)
        if cached is not None and (cached.kind == "movie" or cached.seasons):
            return cached
        kind, media_id = self._kind_id(native_url)
        response = self.http.get(native_url, headers={"Referer": self.base_url})
        if response.status_code != 200:
            raise ProviderError(f"catalogue page returned HTTP {response.status_code}")
        title = cached.title if cached is not None else self._page_title(response.text)
        if kind == "tv":
            details = self._next_details(response.text, media_id)
            title = str(details.get("name") or details.get("original_name") or title).strip()
            seasons: list[tuple[int, str, int]] = []
            for item in details.get("seasons", []):
                if not isinstance(item, dict):
                    continue
                try:
                    season_number = int(item["season_number"])
                    episode_count = int(item["episode_count"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not 0 <= season_number <= 999 or not 1 <= episode_count <= 2000:
                    continue
                raw_name = str(item.get("name") or "").strip()
                season_label = (
                    "Specials"
                    if season_number == 0
                    else raw_name or f"Season {season_number}"
                )
                seasons.append((season_number, season_label[:100], episode_count))
            if not seasons:
                raise ProviderError("the series page did not expose usable seasons")
            result = _Title(
                native_url,
                media_id,
                kind,
                title,
                tuple(sorted(seasons)),
            )
        else:
            result = _Title(native_url, media_id, kind, title)
        if not result.title:
            raise ProviderError("the catalogue page did not expose a title")
        self._titles[native_url] = result
        return result

    def _reader_payload(
        self,
        title: _Title,
        season: int,
        episode: int,
    ) -> dict:
        key = (title.kind, title.media_id, season, episode)
        cached = self._reader_payloads.get(key)
        if cached is not None:
            return cached
        if title.kind == "tv":
            endpoint = f"{self.reader_base_url}api/series"
            params = {
                "id": title.media_id,
                "sa": season,
                "epi": episode,
                "idType": "tmdb",
            }
            referer = (
                f"{self.reader_base_url}series?id={title.media_id}"
                f"&sa={season}&epi={episode}"
            )
        else:
            endpoint = f"{self.reader_base_url}api/films"
            params = {"id": title.media_id, "idType": "tmdb"}
            referer = f"{self.reader_base_url}films?id={title.media_id}"
        response = self.http.get(endpoint, params=params, headers={"Referer": referer})
        if response.status_code == 404:
            payload: dict = {}
        elif response.status_code != 200:
            raise ProviderError(f"reader API returned HTTP {response.status_code}")
        else:
            try:
                value = response.json()
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProviderError("reader API returned malformed JSON") from exc
            payload = value if isinstance(value, dict) and not value.get("error") else {}
        self._reader_payloads[key] = payload
        return payload

    def _available_languages(self, title: _Title, season: int) -> tuple[str, ...]:
        payload = self._reader_payload(title, season, 1)
        available = []
        for code, fields in self._reader_fields.items():
            if any(self._reader_link(payload.get(field)) for field in fields):
                available.append(code)
        return tuple(available)

    def _candidates(
        self,
        kind: str,
        media_id: int,
        season: int,
        episode: int,
        language: str,
    ) -> tuple[EmbedCandidate, ...]:
        fields = self._reader_fields[language]
        candidates = []
        for index, server in enumerate(fields, start=1):
            query = {
                "type": "serie" if kind == "tv" else "movie",
                "tmdb": media_id,
                "server": server,
            }
            if kind == "tv":
                query.update({"sa": season, "epi": episode})
            candidates.append(
                EmbedCandidate(
                    f"Reader {index}",
                    f"{self.reader_stream_url}?{urlencode(query)}",
                )
            )
        return tuple(candidates)

    def _variant(
        self,
        title: _Title,
        season_number: int,
        language: MediaLanguage,
    ) -> CatalogueVariant:
        if title.kind == "movie":
            season_label = "Movie"
        else:
            season_label = next(
                item[1] for item in title.seasons if item[0] == season_number
            )
        return CatalogueVariant(
            name=f"{season_label} - {language.label}",
            url=self._with_variant(title.url, season_number, language.code),
            season=season_label,
            language=language,
        )

    def _normalize_url(self, url: str) -> str:
        value = self._with_scheme(url.strip())
        if not self.matches(value):
            raise ProviderError("this URL is not a CineHD film or series URL")
        parsed = urlparse(value)
        query = [
            (key, item)
            for key, item in parse_qsl(parsed.query)
            if key in {self._season_parameter, self._language_parameter}
        ]
        return urlunparse(
            (
                "https",
                "cinehd.app",
                parsed.path.rstrip("/"),
                "",
                urlencode(sorted(query)),
                "",
            )
        )

    def _native_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def _with_variant(self, url: str, season: int, language: str) -> str:
        parsed = urlparse(self._native_url(url))
        query = [(self._language_parameter, language)]
        if "/tv/" in parsed.path:
            query.append((self._season_parameter, str(season)))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                urlencode(sorted(query)),
                "",
            )
        )

    @staticmethod
    def _with_scheme(url: str) -> str:
        return url if url.startswith(("http://", "https://")) else "https://" + url

    @staticmethod
    def _kind_id(url: str) -> tuple[str, int]:
        match = re.fullmatch(r"/(movie|tv)/(\d+)/?", urlparse(url).path)
        if not match:
            raise ProviderError("the catalogue URL is malformed")
        return match.group(1), int(match.group(2))

    @staticmethod
    def _selected(url: str, parameter: str) -> str:
        return next(
            (
                value.strip().casefold()
                for key, value in parse_qsl(urlparse(url).query)
                if key == parameter
            ),
            "",
        )

    def _selected_season(self, url: str) -> int | None:
        value = self._selected(url, self._season_parameter)
        if not value:
            return None
        try:
            season = int(value)
        except ValueError as exc:
            raise ProviderError("the selected season is malformed") from exc
        if not 0 <= season <= 999:
            raise ProviderError("the selected season is outside accepted bounds")
        return season

    @classmethod
    def _next_details(cls, content: str, media_id: int) -> dict:
        decoder = json.JSONDecoder()
        soup = BeautifulSoup(content, "html.parser")
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if not text.startswith(cls._next_push):
                continue
            argument = text[len(cls._next_push) :]
            if argument.endswith(")"):
                argument = argument[:-1]
            try:
                pushed = json.loads(argument)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(pushed, list)
                or len(pushed) < 2
                or not isinstance(pushed[1], str)
            ):
                continue
            payload = pushed[1]
            position = 0
            while True:
                marker = payload.find('"props":', position)
                if marker < 0:
                    break
                try:
                    value, end = decoder.raw_decode(payload, marker + len('"props":'))
                except json.JSONDecodeError:
                    position = marker + len('"props":')
                    continue
                position = end
                if (
                    isinstance(value, dict)
                    and str(value.get("id", "")) == str(media_id)
                    and isinstance(value.get("seasons"), list)
                ):
                    return value
        raise ProviderError("the series page did not expose usable metadata")

    @staticmethod
    def _page_title(content: str) -> str:
        soup = BeautifulSoup(content, "html.parser")
        heading = soup.find("h1")
        if heading is not None:
            title = heading.get_text(" ", strip=True)
            if title:
                return title
        if soup.title:
            return re.sub(r"\s*[|·-]\s*CineHD.*$", "", soup.title.get_text(" ", strip=True))
        return ""

    @staticmethod
    def _reader_link(value: object) -> bool:
        return isinstance(value, str) and value.startswith("/api/stream?")
