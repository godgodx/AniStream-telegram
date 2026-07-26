from anistream.providers.anime_sama import AnimeSamaProvider
from anistream.providers.base import Provider
from anistream.providers.french_stream import FrenchStreamProvider
from anistream.providers.registry import ProviderRegistry
from anistream.utils.http import HttpClient


def default_providers(http: HttpClient) -> list[Provider]:
    """Return every available provider from one explicit registration point."""

    return [AnimeSamaProvider(http), FrenchStreamProvider(http)]


__all__ = ["AnimeSamaProvider", "FrenchStreamProvider", "ProviderRegistry", "default_providers"]
