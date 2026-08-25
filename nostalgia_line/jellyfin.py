"""Jellyfin client.

Jellyfin's item API is Emby-derived, so the same client works against an Emby
server pointed at the same endpoints. Where Plex hands back
``<Guid id="tmdb://1396"/>``, Jellyfin hands back
``ProviderIds: {"Tmdb": "1396", "Imdb": "tt0903747"}`` — different spelling of
the same fact, normalised in :mod:`nostalgia_line.media`.
"""
from __future__ import annotations

import httpx

from .media import (
    MOVIE,
    SHOW,
    LibrarySource,
    MediaItem,
    MediaSection,
    SourceError,
    int_or_none,
    parse_provider_ids,
)

# Jellyfin's CollectionType -> our library type.
_COLLECTION_TYPES = {
    "tvshows": SHOW,
    "movies": MOVIE,
}

# Fields Jellyfin omits unless asked. ProviderIds is the one that matters.
_FIELDS = ",".join(
    [
        "ProviderIds",
        "Genres",
        "Overview",
        "ProductionYear",
        "Studios",
        "ChildCount",
        "RecursiveItemCount",
        "DateCreated",
        "Path",
    ]
)


class JellyfinClient(LibrarySource):
    """Thin async wrapper over the Jellyfin (and Emby) HTTP API."""

    name = "jellyfin"

    def __init__(self, base_url: str, api_key: str, user_id: str = "", timeout: float = 30.0):
        if not base_url:
            raise SourceError("jellyfin.url is not configured")
        if not api_key:
            raise SourceError("jellyfin.api_key is not configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        # Both spellings are accepted; older Emby builds want the Authorization form.
        return {
            "X-Emby-Token": self.api_key,
            "Authorization": (
                'MediaBrowser Client="Nostalgia Line", Device="Nostalgia Line", '
                f'DeviceId="nostalgia-line", Version="1.0", Token="{self.api_key}"'
            ),
            "Accept": "application/json",
        }

    async def _get(self, client: httpx.AsyncClient, path: str, **params):
        url = f"{self.base_url}{path}"
        try:
            response = await client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise SourceError(f"could not reach Jellyfin at {url}: {exc}") from exc
        if response.status_code in (401, 403):
            raise SourceError("Jellyfin rejected the API key. Check jellyfin.api_key.")
        if response.status_code >= 400:
            raise SourceError(f"Jellyfin returned {response.status_code} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"Jellyfin returned unparseable JSON for {path}: {exc}") from exc

    # -- identity --------------------------------------------------------

    async def ping(self) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            info = await self._get(client, "/System/Info")
        return {
            "friendlyName": info.get("ServerName", "") or "Jellyfin",
            "version": info.get("Version", ""),
            "machineIdentifier": info.get("Id", ""),
        }

    async def _resolve_user(self, client: httpx.AsyncClient) -> str:
        """Item queries are scoped to a user. Prefer an administrator."""
        if self.user_id:
            return self.user_id
        users = await self._get(client, "/Users")
        if not users:
            raise SourceError("Jellyfin reported no users, so the library cannot be listed")
        admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
        self.user_id = (admins or users)[0].get("Id", "")
        if not self.user_id:
            raise SourceError("could not determine a Jellyfin user id")
        return self.user_id

    # -- libraries -------------------------------------------------------

    async def sections(self) -> list[MediaSection]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            user_id = await self._resolve_user(client)
            views = await self._get(client, f"/Users/{user_id}/Views")
        out: list[MediaSection] = []
        for view in views.get("Items", []):
            collection = (view.get("CollectionType") or "").lower()
            out.append(
                MediaSection(
                    key=view.get("Id", ""),
                    title=(view.get("Name") or "").strip(),
                    type=_COLLECTION_TYPES.get(collection, collection),
                    uuid=view.get("Id", ""),
                )
            )
        return out

    async def items(self, section: MediaSection, page_size: int = 500) -> list[MediaItem]:
        """Every item in a library, paged."""
        item_type = "Series" if section.type == SHOW else "Movie"
        raw: list[dict] = []
        async with httpx.AsyncClient(timeout=max(self.timeout, 120.0)) as client:
            user_id = await self._resolve_user(client)
            start = 0
            while True:
                payload = await self._get(
                    client,
                    f"/Users/{user_id}/Items",
                    ParentId=section.key,
                    IncludeItemTypes=item_type,
                    Recursive="true",
                    Fields=_FIELDS,
                    StartIndex=start,
                    Limit=page_size,
                    EnableTotalRecordCount="true",
                )
                page = payload.get("Items", []) or []
                raw.extend(page)
                total = int_or_none(payload.get("TotalRecordCount")) or len(raw)
                start += len(page)
                if not page or start >= total:
                    break

        return [self._to_item(entry, section) for entry in raw]

    def _to_item(self, entry: dict, section: MediaSection) -> MediaItem:
        ids = parse_provider_ids(entry.get("ProviderIds") or {})
        tmdb = ids.get("tmdb")
        studios = entry.get("Studios") or []
        return MediaItem(
            rating_key=entry.get("Id", ""),
            title=(entry.get("Name") or "").strip(),
            type=SHOW if entry.get("Type") == "Series" else MOVIE,
            section=section.title,
            year=int_or_none(entry.get("ProductionYear")),
            tmdb_id=int(tmdb) if tmdb else None,
            tvdb_id=int_or_none(ids.get("tvdb")),
            imdb_id=ids.get("imdb"),
            # RecursiveItemCount counts episodes for a series; ChildCount counts seasons.
            episode_count=int_or_none(entry.get("RecursiveItemCount")) or 0,
            season_count=int_or_none(entry.get("ChildCount")) or 0,
            studio=(studios[0].get("Name", "") if studios else "").strip(),
            summary=(entry.get("Overview") or "").strip(),
            thumb="",
            genres=[g for g in (entry.get("Genres") or []) if g],
            added_at=0,
        )

    async def fetch_library(
        self, wanted: list[str] | None = None, types: tuple[str, ...] = (SHOW,)
    ) -> tuple[list[MediaItem], list[MediaSection]]:
        sections = await self.sections()
        selected = [s for s in sections if s.type in types]
        if wanted:
            wanted_folded = {w.casefold() for w in wanted}
            selected = [s for s in selected if s.title.casefold() in wanted_folded]
        items: list[MediaItem] = []
        for section in selected:
            items.extend(await self.items(section))
        return items, selected
