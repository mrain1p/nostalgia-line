"""Media-server abstraction.

Nostalgia Line does not care which server holds the library — only that every
item arrives with a TMDB id attached, because the whole cascade joins on it
(spec S7). Plex exposes those as ``<Guid id="tmdb://1396"/>``; Jellyfin and Emby
expose the same thing as ``ProviderIds: {"Tmdb": "1396"}``. Both normalise to
:class:`MediaItem` here, and nothing downstream knows the difference.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

SHOW = "show"
MOVIE = "movie"

# Agent ids as they appear in Plex guid strings.
_GUID_PATTERNS = {
    "tmdb": re.compile(r"(?:tmdb|themoviedb)://(\d+)"),
    "tvdb": re.compile(r"(?:tvdb|thetvdb)://(\d+)"),
    "imdb": re.compile(r"imdb://(tt\d+)"),
}


class SourceError(RuntimeError):
    """The media server was unreachable or rejected the request."""


@dataclass
class MediaSection:
    """One library on the server."""

    key: str
    title: str
    type: str
    uuid: str = ""

    @property
    def is_show(self) -> bool:
        return self.type == SHOW

    @property
    def is_movie(self) -> bool:
        return self.type == MOVIE


@dataclass
class MediaItem:
    """One title, normalised across servers."""

    rating_key: str
    title: str
    type: str
    section: str
    year: int | None = None
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    episode_count: int = 0
    season_count: int = 0
    studio: str = ""
    summary: str = ""
    thumb: str = ""
    genres: list[str] = field(default_factory=list)
    added_at: int = 0

    @property
    def is_show(self) -> bool:
        return self.type == SHOW

    @property
    def uid(self) -> str:
        """Stable identity across scans, and across servers.

        Keyed on the agent id rather than the server's own rating key, so a
        manual assignment survives a re-scan - or a move from Plex to Jellyfin.
        """
        if self.tmdb_id:
            return f"tmdb:{self.type}:{self.tmdb_id}"
        if self.tvdb_id:
            return f"tvdb:{self.type}:{self.tvdb_id}"
        if self.imdb_id:
            return f"imdb:{self.type}:{self.imdb_id}"
        return f"local:{self.section}:{self.rating_key}"


def parse_guid_strings(blobs: list[str]) -> dict[str, str]:
    """Pull tmdb/tvdb/imdb ids out of Plex-style guid strings.

    NostalgiaTV stores the same shape in its own index
    (``["imdb://tt0043208", "tmdb://2730", "tvdb://70584"]``), so this doubles as
    the parser for anything that mirrors Plex's format.
    """
    found: dict[str, str] = {}
    for blob in blobs:
        if not blob:
            continue
        for agent, pattern in _GUID_PATTERNS.items():
            if agent in found:
                continue
            match = pattern.search(blob)
            if match:
                found[agent] = match.group(1)
    return found


def parse_provider_ids(provider_ids: dict) -> dict[str, str]:
    """Normalise a Jellyfin/Emby ``ProviderIds`` map.

    Keys arrive inconsistently cased across versions and plugins (``Tmdb``,
    ``TMDB``, ``tmdb``), so match case-insensitively.
    """
    found: dict[str, str] = {}
    for raw_key, raw_value in (provider_ids or {}).items():
        value = str(raw_value or "").strip()
        if not value:
            continue
        key = raw_key.strip().lower()
        if key in ("tmdb", "themoviedb") and value.isdigit():
            found.setdefault("tmdb", value)
        elif key in ("tvdb", "thetvdb") and value.isdigit():
            found.setdefault("tvdb", value)
        elif key == "imdb" and value.startswith("tt"):
            found.setdefault("imdb", value)
    return found


def int_or_none(raw) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return int(text) if text.isdigit() else None


class LibrarySource(Protocol):
    """What the pipeline needs from a media server. Nothing more."""

    name: str

    async def ping(self) -> dict[str, str]:
        """Verify the server is reachable and the credential works."""
        ...

    async def sections(self) -> list[MediaSection]:
        ...

    async def fetch_library(
        self, wanted: list[str] | None = None, types: tuple[str, ...] = (SHOW,)
    ) -> tuple[list[MediaItem], list[MediaSection]]:
        ...
