from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from anistream.errors import ProviderError
from anistream.models import Catalogue, CatalogueVariant, EmbedCandidate, Episode, MediaLanguage, SearchResult
from anistream.providers.base import Provider
from anistream.utils.http import HttpClient


class AnimeSamaProvider(Provider):
    id = "anime_sama"
    name = "Anime-Sama"
    base_url = "https://anime-sama.to/"
    search_url = "https://anime-sama.to/template-php/defaut/fetch.php"
    language_codes = ("vostfr", "vf", "va", "var", "vkr", "vcn", "vqc", "vf1", "vf2")

    _episode_array = re.compile(r"var\s+eps(\d+)\s*=\s*\[(.*?)\]\s*;", re.IGNORECASE | re.DOTALL)
    _quoted_url = re.compile(r"(['\"])(https?://.+?)\1", re.IGNORECASE)
    _panel = re.compile(
        r"panneauAnime\s*\(\s*(['\"])(.*?)\1\s*,\s*(['\"])(.*?)\3\s*\)",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def matches(self, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return False
        return bool(re.search(r"(^|\.)anime-sama\.[a-z0-9.-]+$", host))

    def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []
        response = self.http.post(self.search_url, data={"query": query})
        if response.status_code != 200:
            raise ProviderError(f"search returned HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        results: list[SearchResult] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            url = urljoin(self.base_url, str(anchor["href"]))
            if "/catalogue/" not in url or url in seen:
                continue
            title_node = anchor.find("h3")
            title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)
            if not title:
                continue
            seen.add(url)
            results.append(SearchResult(self.id, self.name, unescape(title), url))
        return results

    def variants(self, url: str) -> list[CatalogueVariant]:
        normalized = self._normalize_url(url)
        parts = self._catalogue_parts(normalized)
        if len(parts) >= 4:
            season = self._season_label(parts[2])
            language = self._language(parts[3])
            return [CatalogueVariant(self._variant_label(parts[2], parts[3]), normalized, season, language)]

        root = self._root_url(normalized)
        response = self.http.get(root)
        if response.status_code != 200:
            raise ProviderError(f"catalogue page returned HTTP {response.status_code}")

        variants: list[CatalogueVariant] = []
        seen: set[str] = set()
        season_roots: dict[str, str] = {}
        for _, name, _, relative in self._panel.findall(self._strip_comments(response.text)):
            if name.strip().lower() == "nom" or relative.strip().lower() == "url":
                continue
            variant_url = urljoin(root, relative.strip()).rstrip("/") + "/"
            if "/scan" in variant_url.lower() or variant_url in seen:
                continue
            seen.add(variant_url)
            variant_parts = self._catalogue_parts(variant_url)
            label = name.strip()
            season_label = ""
            language = None
            if len(variant_parts) >= 4:
                label = self._variant_label(variant_parts[2], variant_parts[3], label)
                season_label = self._season_label(variant_parts[2])
                language = self._language(variant_parts[3])
                parsed = urlparse(variant_url)
                season_roots[variant_parts[2]] = (
                    f"{parsed.scheme}://{parsed.netloc}/catalogue/{variant_parts[1]}/{variant_parts[2]}/"
                )
            variants.append(CatalogueVariant(label, variant_url, season_label, language))

        candidates: dict[str, tuple[str, str]] = {}
        for season, season_root in season_roots.items():
            for language in self.language_codes:
                candidate_url = urljoin(season_root, language).rstrip("/") + "/"
                if candidate_url not in seen:
                    candidates[candidate_url] = (season, language)
        if candidates:
            with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                pending = {pool.submit(self._has_episodes, candidate_url): candidate_url for candidate_url in candidates}
                for future in as_completed(pending):
                    candidate_url = pending[future]
                    if not future.result():
                        continue
                    season, language = candidates[candidate_url]
                    seen.add(candidate_url)
                    variants.append(
                        CatalogueVariant(
                            self._variant_label(season, language),
                            candidate_url,
                            self._season_label(season),
                            self._language(language),
                        )
                    )

        if not variants:
            raise ProviderError("no watchable seasons or language variants were found")
        variants.sort(key=lambda item: self._variant_sort_key(item.url))
        return variants

    def catalogue(self, url: str) -> Catalogue:
        normalized = self._normalize_url(url)
        parts = self._catalogue_parts(normalized)
        if len(parts) < 4:
            raise ProviderError("select a season and language before loading episodes")

        episodes_url = normalized.rstrip("/") + "/episodes.js"
        response = self.http.get(episodes_url)
        if response.status_code != 200:
            raise ProviderError(f"episode list returned HTTP {response.status_code}")

        players: dict[int, list[str]] = {}
        for player_number, body in self._episode_array.findall(response.text):
            urls = [match[1].strip() for match in self._quoted_url.findall(body)]
            players[int(player_number)] = [value for value in urls if self._real_embed(value)]

        episode_count = max((len(urls) for urls in players.values()), default=0)
        if episode_count == 0:
            raise ProviderError("the episode list did not contain usable embed links")

        episodes: list[Episode] = []
        for index in range(episode_count):
            candidates = tuple(
                EmbedCandidate(player=f"Player {number}", url=urls[index])
                for number, urls in sorted(players.items())
                if index < len(urls) and self._real_embed(urls[index])
            )
            episodes.append(Episode(number=index + 1, candidates=candidates))

        title = self._title_from_slug(parts[1])
        return Catalogue(
            provider_id=self.id,
            provider_name=self.name,
            title=title,
            url=normalized,
            season=self._season_label(parts[2]),
            language=self._language(parts[3]),
            episodes=tuple(episodes),
        )

    def _normalize_url(self, url: str) -> str:
        value = url.strip()
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        if not self.matches(value):
            raise ProviderError("this URL is not an Anime-Sama URL")
        parsed = urlparse(value)
        path = re.sub(r"/{2,}", "/", parsed.path)
        if "/catalogue/" not in path:
            raise ProviderError("the URL does not point to an Anime-Sama catalogue page")
        return f"{parsed.scheme}://{parsed.netloc}{path.rstrip('/')}/"

    def _has_episodes(self, url: str) -> bool:
        try:
            response = self.http.get(url.rstrip("/") + "/episodes.js", timeout=(5, 10))
            return response.status_code == 200 and bool(self._episode_array.search(response.text))
        except Exception:
            return False

    def _root_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = self._catalogue_parts(url)
        if len(parts) < 2:
            raise ProviderError("the catalogue URL is missing a title slug")
        return f"{parsed.scheme}://{parsed.netloc}/catalogue/{parts[1]}/"

    @staticmethod
    def _catalogue_parts(url: str) -> list[str]:
        return [part for part in urlparse(url).path.split("/") if part]

    @staticmethod
    def _strip_comments(content: str) -> str:
        content = re.sub(r"<!--[\s\S]*?-->", "", content)
        return re.sub(r"/\*[\s\S]*?\*/", "", content)

    @staticmethod
    def _variant_label(season: str, language: str, prefix: str = "") -> str:
        return f"{AnimeSamaProvider._season_label(season)} - {language.upper()}"

    @staticmethod
    def _language(code: str) -> MediaLanguage:
        # Codes belong to Anime-Sama. The application core treats them as opaque values.
        return MediaLanguage.from_code(code)

    @staticmethod
    def _season_label(season: str) -> str:
        normalized = re.sub(r"(?i)^saison", "Season ", season).replace("-", " ").strip()
        return normalized.title()

    @staticmethod
    def _title_from_slug(slug: str) -> str:
        return re.sub(r"[-_]+", " ", slug).strip().title()

    @staticmethod
    def _variant_sort_key(url: str) -> tuple[int, str]:
        parts = AnimeSamaProvider._catalogue_parts(url)
        season = parts[2] if len(parts) > 2 else ""
        match = re.search(r"(\d+)", season)
        return (int(match.group(1)) if match else 9999, parts[3] if len(parts) > 3 else "")

    @staticmethod
    def _real_embed(url: str) -> bool:
        value = url.strip().lower()
        if len(value) < 20 or not value.startswith(("http://", "https://")):
            return False
        if re.search(r"[?&][a-z0-9_]+=(?:$|&)", value):
            return False
        return not value.endswith(("/embed", "/embed/"))
