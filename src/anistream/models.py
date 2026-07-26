from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MediaLanguage:
    """Provider-owned language metadata carried through the neutral core."""

    code: str
    label: str

    def __post_init__(self) -> None:
        code = self.code.strip().casefold()
        label = self.label.strip()
        if not code or not label:
            raise ValueError("Media language code and label must not be empty")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "label", label)

    @classmethod
    def from_code(cls, code: str, label: str | None = None) -> MediaLanguage:
        normalized = code.strip()
        return cls(normalized, label or normalized.upper())


@dataclass(frozen=True, slots=True)
class SearchResult:
    provider_id: str
    provider_name: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class CatalogueVariant:
    name: str
    url: str
    season: str = ""
    language: MediaLanguage | None = None


@dataclass(frozen=True, slots=True)
class EmbedCandidate:
    player: str
    url: str


@dataclass(frozen=True, slots=True)
class Episode:
    number: int
    candidates: tuple[EmbedCandidate, ...]


@dataclass(frozen=True, slots=True)
class Catalogue:
    provider_id: str
    provider_name: str
    title: str
    url: str
    season: str
    language: MediaLanguage
    episodes: tuple[Episode, ...]


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    url: str
    embed_url: str
    resolver_name: str
    headers: Mapping[str, str] = field(default_factory=dict)
    kind: str = "unknown"


@dataclass(slots=True)
class SourceAttempt:
    player: str
    embed_host: str
    resolver: str | None = None
    success: bool = False
    detail: str = ""


@dataclass(slots=True)
class DownloadResult:
    episode: int
    success: bool
    output: Path | None = None
    source: str | None = None
    validation: str = "not run"
    attempts: list[SourceAttempt] = field(default_factory=list)
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class ProbeResult:
    valid: bool
    kind: str = "unknown"
    detail: str = ""
