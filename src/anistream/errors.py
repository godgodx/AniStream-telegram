class AniStreamError(Exception):
    """Base exception for expected application errors."""


class ProviderError(AniStreamError):
    """A catalogue provider could not complete an operation."""


class ResolverError(AniStreamError):
    """An embed host could not be converted into a playable media URL."""
