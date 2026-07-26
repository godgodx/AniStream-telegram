from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from anistream.models import SearchResult
from anistream.providers.base import Provider


class ProviderRegistry:
    def __init__(self, providers: list[Provider] | None = None) -> None:
        self._providers = list(providers or [])

    def register(self, provider: Provider) -> None:
        if any(item.id == provider.id for item in self._providers):
            raise ValueError(f"Provider already registered: {provider.id}")
        self._providers.append(provider)

    def detect(self, url: str) -> Provider | None:
        return next((provider for provider in self._providers if provider.matches(url)), None)

    def get(self, provider_id: str) -> Provider | None:
        return next((provider for provider in self._providers if provider.id == provider_id), None)

    def search(self, query: str) -> tuple[list[SearchResult], list[str]]:
        results: list[SearchResult] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self._providers))) as pool:
            pending = {pool.submit(provider.search, query): provider for provider in self._providers}
            for future in as_completed(pending):
                provider = pending[future]
                try:
                    results.extend(future.result())
                except Exception as exc:
                    errors.append(f"{provider.name}: {exc}")
        results.sort(key=lambda item: (item.title.lower(), item.provider_name.lower()))
        return results, errors

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers)

    def names(self) -> str:
        return ", ".join(provider.name for provider in self._providers)
