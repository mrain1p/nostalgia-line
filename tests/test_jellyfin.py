"""Jellyfin adapter.

There is no Jellyfin server to test against, so these pin the contract: the
response shapes are taken from Jellyfin's documented API, and the assertions are
about normalising them into the same MediaItem Plex produces. If Jellyfin ever
changes shape, these are what should be re-checked first.
"""
import asyncio

import pytest

from nostalgia_line.config import Config, JellyfinConfig, PlexConfig
from nostalgia_line.jellyfin import JellyfinClient
from nostalgia_line.media import (
    MOVIE,
    SHOW,
    MediaSection,
    SourceError,
    parse_guid_strings,
    parse_provider_ids,
)
from nostalgia_line.plex import PlexClient
from nostalgia_line.sources import (
    build_source,
    missing_credential_message,
    source_is_configured,
    source_libraries,
)


# -- provider id normalisation -------------------------------------------


def test_provider_ids_are_normalised():
    ids = parse_provider_ids({"Tmdb": "1396", "Imdb": "tt0903747", "Tvdb": "81189"})
    assert ids == {"tmdb": "1396", "imdb": "tt0903747", "tvdb": "81189"}


def test_provider_id_keys_are_case_insensitive():
    """Casing varies across Jellyfin versions and metadata plugins."""
    assert parse_provider_ids({"TMDB": "1396"})["tmdb"] == "1396"
    assert parse_provider_ids({"tmdb": "1396"})["tmdb"] == "1396"


def test_blank_and_malformed_provider_ids_are_dropped():
    ids = parse_provider_ids({"Tmdb": "", "Imdb": "not-an-id", "Tvdb": "abc", "Zap2It": "x"})
    assert ids == {}


def test_jellyfin_and_plex_agree_on_the_same_title():
    """The whole point of the abstraction: one join key, two servers."""
    plex_ids = parse_guid_strings(["tmdb://1396", "imdb://tt0903747"])
    jelly_ids = parse_provider_ids({"Tmdb": "1396", "Imdb": "tt0903747"})
    assert plex_ids["tmdb"] == jelly_ids["tmdb"] == "1396"


# -- fake transport ------------------------------------------------------

SERIES = [
    {
        "Id": "a1", "Name": "Breaking Bad", "Type": "Series", "ProductionYear": 2008,
        "ProviderIds": {"Tmdb": "1396", "Imdb": "tt0903747", "Tvdb": "81189"},
        "Genres": ["Drama", "Crime"], "Overview": "A chemistry teacher.",
        "RecursiveItemCount": 62, "ChildCount": 5, "Studios": [{"Name": "AMC"}],
    },
    {
        "Id": "a2", "Name": "No Ids Here", "Type": "Series", "ProductionYear": 2020,
        "ProviderIds": {}, "Genres": [], "Overview": "",
        "RecursiveItemCount": 8, "ChildCount": 1, "Studios": [],
    },
]


class FakeJellyfin(JellyfinClient):
    """Intercepts _get so no HTTP happens."""

    def __init__(self, total=None, page_size=500, **kw):
        super().__init__("http://jelly:8096", "key", **kw)
        self.total = len(SERIES) if total is None else total
        self.page_size = page_size
        self.calls: list[tuple[str, dict]] = []

    async def _get(self, client, path, **params):
        self.calls.append((path, params))
        if path == "/System/Info":
            return {"ServerName": "Fake Jelly", "Version": "10.9.0", "Id": "srv1"}
        if path == "/Users":
            return [
                {"Id": "u-normal", "Name": "kid", "Policy": {"IsAdministrator": False}},
                {"Id": "u-admin", "Name": "boss", "Policy": {"IsAdministrator": True}},
            ]
        if path.endswith("/Views"):
            return {
                "Items": [
                    {"Id": "lib-tv", "Name": "Shows", "CollectionType": "tvshows"},
                    {"Id": "lib-mv", "Name": "Films", "CollectionType": "movies"},
                    {"Id": "lib-mu", "Name": "Music", "CollectionType": "music"},
                ]
            }
        if path.endswith("/Items"):
            start = int(params.get("StartIndex", 0))
            limit = int(params.get("Limit", self.page_size))
            rows = []
            for i in range(start, min(start + limit, self.total)):
                base = dict(SERIES[i % len(SERIES)])
                base["Id"] = f"item-{i}"
                if base["ProviderIds"]:
                    base["ProviderIds"] = {**base["ProviderIds"], "Tmdb": str(1000 + i)}
                rows.append(base)
            return {"Items": rows, "TotalRecordCount": self.total}
        raise AssertionError(f"unexpected path {path}")


def run(coro):
    return asyncio.run(coro)


# -- client behaviour ----------------------------------------------------


def test_requires_url_and_key():
    with pytest.raises(SourceError):
        JellyfinClient("", "key")
    with pytest.raises(SourceError):
        JellyfinClient("http://jelly:8096", "")


def test_ping_reports_the_server():
    info = run(FakeJellyfin().ping())
    assert info["friendlyName"] == "Fake Jelly"
    assert info["version"] == "10.9.0"


def test_sections_map_collection_types():
    sections = run(FakeJellyfin().sections())
    by_name = {s.title: s for s in sections}
    assert by_name["Shows"].type == SHOW
    assert by_name["Films"].type == MOVIE
    assert by_name["Music"].type == "music", "unknown types pass through, not crash"


def test_an_admin_user_is_preferred():
    client = FakeJellyfin()
    run(client.sections())
    assert client.user_id == "u-admin"


def test_an_explicit_user_id_is_respected():
    client = FakeJellyfin(user_id="u-chosen")
    run(client.sections())
    assert client.user_id == "u-chosen"
    assert not any(path == "/Users" for path, _ in client.calls)


def test_items_normalise_to_media_items():
    section = MediaSection(key="lib-tv", title="Shows", type=SHOW)
    items = run(FakeJellyfin().items(section))
    first = items[0]
    assert first.title == "Breaking Bad"
    assert first.tmdb_id == 1000
    assert first.imdb_id == "tt0903747"
    assert first.tvdb_id == 81189
    assert first.type == SHOW
    assert first.section == "Shows"
    assert first.year == 2008
    assert first.episode_count == 62, "RecursiveItemCount is the episode count"
    assert first.season_count == 5, "ChildCount is the season count"
    assert first.studio == "AMC"
    assert first.genres == ["Drama", "Crime"]


def test_an_item_with_no_provider_ids_still_arrives():
    """It must reach the review queue, not vanish before the cascade sees it."""
    section = MediaSection(key="lib-tv", title="Shows", type=SHOW)
    items = run(FakeJellyfin().items(section))
    orphan = next(i for i in items if i.title == "No Ids Here")
    assert orphan.tmdb_id is None
    assert orphan.uid.startswith("local:")


def test_uid_matches_what_plex_would_produce():
    """A title keeps its identity if the user migrates Plex -> Jellyfin."""
    section = MediaSection(key="lib-tv", title="Shows", type=SHOW)
    item = run(FakeJellyfin().items(section))[0]
    assert item.uid == "tmdb:show:1000"


def test_large_libraries_are_paged():
    section = MediaSection(key="lib-tv", title="Shows", type=SHOW)
    client = FakeJellyfin(total=1200)
    items = run(client.items(section, page_size=500))
    assert len(items) == 1200
    windows = [
        (p.get("StartIndex"), p.get("Limit")) for path, p in client.calls if path.endswith("/Items")
    ]
    assert windows == [(0, 500), (500, 500), (1000, 500)]
    assert len({i.uid for i in items}) == 1200


def test_an_empty_library_terminates():
    section = MediaSection(key="lib-tv", title="Shows", type=SHOW)
    assert run(FakeJellyfin(total=0).items(section)) == []


def test_fetch_library_selects_shows_only_by_default():
    items, sections = run(FakeJellyfin().fetch_library())
    assert [s.title for s in sections] == ["Shows"]
    assert all(i.type == SHOW for i in items)


def test_fetch_library_honours_the_opt_in_list():
    _, sections = run(FakeJellyfin().fetch_library(wanted=["Films"], types=(SHOW, MOVIE)))
    assert [s.title for s in sections] == ["Films"]


# -- the source factory --------------------------------------------------


def test_factory_builds_the_selected_source():
    plex_cfg = Config(source="plex", plex=PlexConfig(url="http://p:32400", token="t"))
    jelly_cfg = Config(
        source="jellyfin", jellyfin=JellyfinConfig(url="http://j:8096", api_key="k")
    )
    assert isinstance(build_source(plex_cfg), PlexClient)
    assert isinstance(build_source(jelly_cfg), JellyfinClient)


def test_factory_rejects_an_unknown_source():
    with pytest.raises(SourceError):
        build_source(Config(source="betamax"))


def test_configured_check_follows_the_selected_source():
    cfg = Config(source="jellyfin", plex=PlexConfig(url="http://p", token="t"))
    assert source_is_configured(cfg) is False, "plex creds must not satisfy jellyfin"
    cfg.jellyfin = JellyfinConfig(url="http://j:8096", api_key="k")
    assert source_is_configured(cfg) is True


def test_library_opt_in_follows_the_selected_source():
    cfg = Config(
        source="jellyfin",
        plex=PlexConfig(libraries=["PlexShows"]),
        jellyfin=JellyfinConfig(libraries=["JellyShows"]),
    )
    assert source_libraries(cfg) == ["JellyShows"]
    cfg.source = "plex"
    assert source_libraries(cfg) == ["PlexShows"]


def test_missing_credential_message_names_the_right_server():
    assert "Jellyfin" in missing_credential_message(Config(source="jellyfin"))
    assert "Plex" in missing_credential_message(Config(source="plex"))
