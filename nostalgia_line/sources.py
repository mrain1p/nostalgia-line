"""Pick the media server the library comes from.

NostalgiaTV itself is deliberately not a source. Its server API is closed,
undocumented and licensed, and it ships on a moving tag; the CSV it imports and
exports is a far more stable contract. So Nostalgia Line reads the library from
the same place NostalgiaTV does, and exchanges channel assignments as files.
"""
from __future__ import annotations

from .config import Config
from .jellyfin import JellyfinClient
from .media import LibrarySource, SourceError
from .plex import PlexClient


def build_source(cfg: Config) -> LibrarySource:
    """Construct the configured library source."""
    if cfg.source == "jellyfin":
        return JellyfinClient(
            cfg.jellyfin.url, cfg.jellyfin.api_key, user_id=cfg.jellyfin.user_id
        )
    if cfg.source == "plex":
        return PlexClient(cfg.plex.url, cfg.plex.token)
    raise SourceError(f"unknown source {cfg.source!r}")


def source_libraries(cfg: Config) -> list[str]:
    """The library names the user opted into, for the active source."""
    if cfg.source == "jellyfin":
        return list(cfg.jellyfin.libraries)
    return list(cfg.plex.libraries)


def source_is_configured(cfg: Config) -> bool:
    if cfg.source == "jellyfin":
        return bool(cfg.jellyfin.url and cfg.jellyfin.api_key)
    return bool(cfg.plex.url and cfg.plex.token)


def missing_credential_message(cfg: Config) -> str:
    if cfg.source == "jellyfin":
        return "Jellyfin URL and API key must be set first"
    return "Plex URL and token must be set first"
