"""Poster cache and channel logos.

The poster path arrives from a query string, so the validation here is a security
boundary as much as a correctness one: it must never address a file outside the
cache directory or a host other than TMDB.
"""
import asyncio

import pytest

from nostalgia_line.posters import ALLOWED_SIZES, DEFAULT_SIZE, PosterCache


# -- path validation -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
        "/abc-123_XY.png",
        "/x.webp",
    ],
)
def test_real_poster_paths_are_accepted(path):
    assert PosterCache.valid(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        None,
        "../../etc/passwd",
        "/../../config.yaml",
        "/nested/dir/poster.jpg",
        "https://evil.example/x.jpg",
        "//evil.example/x.jpg",
        "/poster.jpg?x=1",
        "/poster.exe",
        "/poster",
        "/a b.jpg",
    ],
)
def test_anything_else_is_rejected(path):
    assert PosterCache.valid(path) is False


def test_cache_filename_stays_inside_the_directory(tmp_path):
    cache = PosterCache(tmp_path)
    target = cache.path_for("/abc.jpg", "w92")
    assert target.parent.resolve() == tmp_path.resolve()
    assert target.name == "w92_abc.jpg"


def test_size_is_part_of_the_cache_key(tmp_path):
    cache = PosterCache(tmp_path)
    assert cache.path_for("/abc.jpg", "w92") != cache.path_for("/abc.jpg", "w342")


# -- fetching and caching ------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, content=b"\xff\xd8\xffJPEGDATA"):
        self.status_code = status
        self.content = content


class FakeClient:
    """Counts requests so the caching claim can actually be checked."""

    calls: list[str] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        FakeClient.calls.append(url)
        return FakeResponse()


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr("nostalgia_line.posters.httpx.AsyncClient", FakeClient)
    return FakeClient


def test_a_poster_is_downloaded_once_then_served_from_disk(tmp_path, fake_http):
    cache = PosterCache(tmp_path)
    first = asyncio.run(cache.fetch("/abc.jpg", "w92"))
    second = asyncio.run(cache.fetch("/abc.jpg", "w92"))
    assert first == second
    assert first.exists()
    assert len(fake_http.calls) == 1, "second read must not hit the network"
    assert fake_http.calls[0] == "https://image.tmdb.org/t/p/w92/abc.jpg"


def test_concurrent_requests_download_once(tmp_path, fake_http):
    """A table paints many rows at once; they must not all start the same fetch."""
    cache = PosterCache(tmp_path)

    async def race():
        return await asyncio.gather(*(cache.fetch("/abc.jpg", "w92") for _ in range(8)))

    results = asyncio.run(race())
    assert len({str(r) for r in results}) == 1
    assert len(fake_http.calls) == 1


def test_an_unknown_size_falls_back_to_the_default(tmp_path, fake_http):
    cache = PosterCache(tmp_path)
    asyncio.run(cache.fetch("/abc.jpg", "w9999"))
    assert f"/t/p/{DEFAULT_SIZE}/abc.jpg" in fake_http.calls[0]


def test_every_allowed_size_is_a_tmdb_size():
    assert DEFAULT_SIZE in ALLOWED_SIZES
    assert all(s.startswith("w") and s[1:].isdigit() for s in ALLOWED_SIZES)


def test_an_invalid_path_never_reaches_the_network(tmp_path, fake_http):
    cache = PosterCache(tmp_path)
    assert asyncio.run(cache.fetch("../../etc/passwd")) is None
    assert fake_http.calls == []


def test_stats_and_clear(tmp_path, fake_http):
    cache = PosterCache(tmp_path)
    asyncio.run(cache.fetch("/abc.jpg", "w92"))
    asyncio.run(cache.fetch("/def.jpg", "w92"))
    assert cache.stats()["count"] == 2
    assert cache.stats()["bytes"] > 0
    assert cache.clear() == 2
    assert cache.stats()["count"] == 0


class FailingClient(FakeClient):
    async def get(self, url):
        FakeClient.calls.append(url)
        return FakeResponse(status=404, content=b"")


def test_a_missing_poster_returns_nothing_and_caches_nothing(tmp_path, monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr("nostalgia_line.posters.httpx.AsyncClient", FailingClient)
    cache = PosterCache(tmp_path)
    assert asyncio.run(cache.fetch("/gone.jpg", "w92")) is None
    assert cache.stats()["count"] == 0, "a 404 must not leave a zero-byte file behind"


# -- cache schema versioning ---------------------------------------------


def test_an_entry_from_an_older_schema_is_refetched(tmp_path):
    """The bug this prevents: a warm cache written before a field existed made
    the new field silently empty forever, because nothing ever refetched it."""
    import json

    from nostalgia_line.tmdb import CACHE_SCHEMA, TMDBCache

    (tmp_path / "tmdb_series.json").write_text(
        json.dumps({"1396": {"tmdb_id": 1396, "name": "Old", "networks": ["AMC"]}})
    )
    cache = TMDBCache(tmp_path)
    assert cache.get("series", 1396) is None, "a pre-schema entry must be a miss"
    assert cache.stale_count("series") == 1

    cache.put("series", 1396, {"tmdb_id": 1396, "name": "New"})
    fresh = cache.get("series", 1396)
    assert fresh is not None
    assert fresh["_schema"] == CACHE_SCHEMA
    assert cache.stale_count("series") == 0


def test_network_logos_survive_a_cache_round_trip(tmp_path):
    from nostalgia_line.tmdb import TMDBCache, TMDBSeries

    cache = TMDBCache(tmp_path)
    series = TMDBSeries(
        tmdb_id=1396, name="Breaking Bad", networks=["AMC"],
        network_logos={"AMC": "/pmvRmATOCaDykE6JrVoeYxlFHw3.png"},
    )
    cache.put("series", 1396, series.to_dict())
    cache.flush()

    restored = TMDBSeries.from_dict(TMDBCache(tmp_path).get("series", 1396))
    assert restored.network_logos == {"AMC": "/pmvRmATOCaDykE6JrVoeYxlFHw3.png"}
