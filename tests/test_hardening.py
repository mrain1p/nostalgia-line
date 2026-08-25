"""Robustness: Plex paging, cache durability, staleness and cancellation."""
import asyncio
from xml.etree import ElementTree as ET

import pytest

from nostalgia_line.plex import PlexClient, PlexError, PlexSection
from nostalgia_line.tmdb import TMDBCache


# -- Plex paging ---------------------------------------------------------


class _PagingPlex(PlexClient):
    """Records the container windows Plex was asked for."""

    def __init__(self, total: int, page_size: int = 500):
        super().__init__("http://fake:32400", "token")
        self.total = total
        self.page_size = page_size
        self.windows: list[tuple[int, int]] = []

    async def _get_xml(self, client, path, **params):
        start = int(params.get("X-Plex-Container-Start", 0))
        size = int(params.get("X-Plex-Container-Size", self.page_size))
        self.windows.append((start, size))
        count = max(0, min(size, self.total - start))
        rows = "".join(
            f'<Directory ratingKey="{start + i}" title="Show {start + i}" type="show" '
            f'year="2020"><Guid id="tmdb://{9000 + start + i}"/></Directory>'
            for i in range(count)
        )
        return ET.fromstring(f'<MediaContainer totalSize="{self.total}">{rows}</MediaContainer>')


def test_large_library_is_fetched_in_pages():
    plex = _PagingPlex(total=1200, page_size=500)
    section = PlexSection(key="1", title="Shows", type="show")
    items = asyncio.run(plex.items(section, page_size=500))
    assert len(items) == 1200
    assert plex.windows == [(0, 500), (500, 500), (1000, 500)]
    assert items[0].tmdb_id == 9000
    assert items[-1].tmdb_id == 9000 + 1199


def test_an_exactly_full_page_does_not_loop_forever():
    plex = _PagingPlex(total=500, page_size=500)
    items = asyncio.run(plex.items(PlexSection(key="1", title="Shows", type="show"), page_size=500))
    assert len(items) == 500
    assert plex.windows == [(0, 500)]


def test_an_empty_library_terminates():
    plex = _PagingPlex(total=0, page_size=500)
    items = asyncio.run(plex.items(PlexSection(key="1", title="Shows", type="show")))
    assert items == []


def test_uids_are_unique_across_pages():
    plex = _PagingPlex(total=700, page_size=250)
    items = asyncio.run(plex.items(PlexSection(key="1", title="Shows", type="show"), page_size=250))
    assert len({i.uid for i in items}) == 700


def test_client_refuses_to_build_without_credentials():
    with pytest.raises(PlexError):
        PlexClient("", "token")
    with pytest.raises(PlexError):
        PlexClient("http://fake:32400", "")


# -- TMDB cache durability -----------------------------------------------


def test_cache_round_trips_through_disk(tmp_path):
    cache = TMDBCache(tmp_path)
    cache.put("series", 42, {"tmdb_id": 42, "name": "Cached"})
    cache.flush()

    reopened = TMDBCache(tmp_path)
    assert reopened.get("series", 42)["name"] == "Cached"
    assert reopened.stats()["series"] == 1


def test_unflushed_entries_are_lost_but_flushed_ones_survive(tmp_path):
    """A mid-scan crash must not cost every response fetched so far."""
    cache = TMDBCache(tmp_path)
    cache.put("series", 1, {"tmdb_id": 1})
    cache.flush()
    cache.put("series", 2, {"tmdb_id": 2})  # never flushed

    reopened = TMDBCache(tmp_path)
    assert reopened.get("series", 1) is not None
    assert reopened.get("series", 2) is None


def test_cache_survives_a_corrupt_file(tmp_path):
    (tmp_path / "tmdb_series.json").write_text("{not json", encoding="utf-8")
    cache = TMDBCache(tmp_path)
    assert cache.get("series", 1) is None
    cache.put("series", 1, {"tmdb_id": 1})
    cache.flush()
    assert TMDBCache(tmp_path).get("series", 1) is not None


def test_cache_clear_removes_everything(tmp_path):
    cache = TMDBCache(tmp_path)
    cache.put("series", 1, {"tmdb_id": 1})
    cache.put("network", 49, {"results": []})
    cache.flush()
    cache.clear()
    assert set(TMDBCache(tmp_path).stats().values()) == {0}
    assert "network" in TMDBCache(tmp_path).stats()
