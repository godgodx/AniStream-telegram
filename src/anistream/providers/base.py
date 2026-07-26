from __future__ import annotations

from abc import ABC, abstractmethod

from anistream.models import Catalogue, CatalogueVariant, SearchResult


class Provider(ABC):
    id: str
    name: str

    @abstractmethod
    def matches(self, url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str) -> list[SearchResult]:
        raise NotImplementedError

    @abstractmethod
    def variants(self, url: str) -> list[CatalogueVariant]:
        raise NotImplementedError

    @abstractmethod
    def catalogue(self, url: str) -> Catalogue:
        raise NotImplementedError
