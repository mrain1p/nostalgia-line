"""Plex client - enumerate libraries and pull items with their tmdb guids (spec S2, S13.1).

The shared types live in :mod:`nostalgia_line.media`; this module only knows how
to talk to Plex. The ``Plex*`` aliases below keep older imports working.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

import httpx

from .media import (
    MOVIE,
    SHOW,
    LibrarySource,
    MediaItem,
    MediaSection,
    SourceError,
    int_or_none,
    parse_guid_strings,
)

# Backwards-compatible aliases.
PlexError = SourceError
PlexItem = MediaItem
PlexSection = MediaSection


def _extract_guids(element: ET.Element) -> dict[str, str]:
    """Pull agent ids from an item's Guid children and its legacy guid attribute."""
    blobs = [child.get("id", "") for child in element.findall("Guid")]
    blobs.append(element.get("guid", ""))
    return parse_guid_strings(blobs)


def _int_or_none(raw: str | None) -> int | None:
    return int_or_none(raw)


class PlexClient(LibrarySource):
    """Thin async wrapper over the Plex HTTP API."""

    name = "plex"

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        if not base_url:
            raise SourceError("plex.url is not configured")
        if not token:
            raise SourceError("plex.token is not configured")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "X-Plex-Token": self.token,
            "Accept": "application/xml",
            "X-Plex-Product": "Nostalgia Line",
            "X-Plex-Client-Identifier": "nostalgia-line",
        }

    async def _get_xml(self, client: httpx.AsyncClient, path: str, **params) -> ET.Element:
        url = f"{self.base_url}{path}"
        try:
            response = await client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SourceError(f"could not reach Plex at {url}: {exc}") from exc
        if response.status_code == 401:
            raise SourceError("Plex rejected the token (401). Check plex.token.")
        if response.status_code >= 400:
            raise SourceError(f"Plex returned {response.status_code} for {path}")
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise SourceError(f"Plex returned unparseable XML for {path}: {exc}") from exc

    async def ping(self) -> dict[str, str]:
        """Verify the server is reachable and the token works."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            root = await self._get_xml(client, "/")
            return {
                "friendlyName": root.get("friendlyName", ""),
                "version": root.get("version", ""),
                "machineIdentifier": root.get("machineIdentifier", ""),
            }

    async def sections(self) -> list[MediaSection]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            root = await self._get_xml(client, "/library/sections")
        return [
            MediaSection(
                key=directory.get("key", ""),
                title=directory.get("title", ""),
                type=directory.get("type", ""),
                uuid=directory.get("uuid", ""),
            )
            for directory in root.findall("Directory")
        ]

    async def items(self, section: MediaSection, page_size: int = 500) -> list[MediaItem]:
        """Every item in a section, with guids included.

        Paged: asking a large library for everything at once makes Plex build one
        enormous XML document, which is slow and occasionally times out.
        """
        elements: list[ET.Element] = []
        async with httpx.AsyncClient(timeout=max(self.timeout, 120.0)) as client:
            start = 0
            while True:
                root = await self._get_xml(
                    client,
                    f"/library/sections/{section.key}/all",
                    includeGuids=1,
                    **{
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": page_size,
                    },
                )
                page = list(root.findall("Directory")) + list(root.findall("Video"))
                elements.extend(page)
                total = _int_or_none(root.get("totalSize")) or len(page)
                start += len(page)
                if not page or start >= total:
                    break

        items: list[MediaItem] = []
        for element in elements:
            guids = _extract_guids(element)
            tmdb = guids.get("tmdb")
            items.append(
                MediaItem(
                    rating_key=element.get("ratingKey", ""),
                    title=(element.get("title") or "").strip(),
                    type=element.get("type") or section.type,
                    section=section.title,
                    year=_int_or_none(element.get("year")),
                    tmdb_id=int(tmdb) if tmdb else None,
                    tvdb_id=_int_or_none(guids.get("tvdb")),
                    imdb_id=guids.get("imdb"),
                    episode_count=_int_or_none(element.get("leafCount")) or 0,
                    season_count=_int_or_none(element.get("childCount")) or 0,
                    studio=(element.get("studio") or "").strip(),
                    summary=(element.get("summary") or "").strip(),
                    thumb=(element.get("thumb") or "").strip(),
                    genres=[g.get("tag", "") for g in element.findall("Genre") if g.get("tag")],
                    added_at=_int_or_none(element.get("addedAt")) or 0,
                )
            )
        return items

    async def fetch_library(
        self, wanted: list[str] | None = None, types: tuple[str, ...] = (SHOW,)
    ) -> tuple[list[MediaItem], list[MediaSection]]:
        """Pull every opted-in section. Defaults to shows only (1.0 scope)."""
        sections = await self.sections()
        selected = [s for s in sections if s.type in types]
        if wanted:
            wanted_folded = {w.casefold() for w in wanted}
            selected = [s for s in selected if s.title.casefold() in wanted_folded]
        items: list[MediaItem] = []
        for section in selected:
            items.extend(await self.items(section))
        return items, selected
