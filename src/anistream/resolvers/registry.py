from __future__ import annotations

from anistream.errors import ResolverError
from anistream.models import ResolvedMedia
from anistream.resolvers.base import Resolver


class ResolverRegistry:
    def __init__(self, resolvers: list[Resolver] | None = None) -> None:
        self._resolvers = list(resolvers or [])

    def register(self, resolver: Resolver) -> None:
        self._resolvers.append(resolver)

    def resolver_for(self, url: str) -> Resolver | None:
        return next((resolver for resolver in self._resolvers if resolver.matches(url)), None)

    def supports(self, url: str) -> bool:
        return self.resolver_for(url) is not None

    def resolve(self, url: str) -> ResolvedMedia:
        resolver = self.resolver_for(url)
        if resolver is None:
            raise ResolverError("unsupported embed host")
        return resolver.resolve(url)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(resolver.name for resolver in self._resolvers)
