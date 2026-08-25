"""End-to-end pipeline against a fake Plex and a fake TMDB (spec S13)."""
import asyncio

import pytest

from nostalgia_line.cascade import STATUS_APP, STATUS_LINE, STATUS_UNASSIGNED
from nostalgia_line.config import Config, DataConfig, PlexConfig, TMDBConfig
from nostalgia_line.pipeline import ScanResult, apply_override, run_scan
from nostalgia_line.plex import PlexItem, PlexSection, _extract_guids
from nostalgia_line.stations import CustomStation, StationBook
from nostalgia_line.store import Store
from nostalgia_line.tmdb import TMDBSeries
from xml.etree import ElementTree as ET

from .conftest import DATA


# -- Plex guid parsing ---------------------------------------------------


def test_guid_extraction_reads_modern_guid_children():
    element = ET.fromstring(
        '<Directory ratingKey="1" title="X" guid="plex://show/abc">'
        '<Guid id="tmdb://244442"/><Guid id="tvdb://999"/><Guid id="imdb://tt1234567"/>'
        "</Directory>"
    )
    guids = _extract_guids(element)
    assert guids["tmdb"] == "244442"
    assert guids["tvdb"] == "999"
    assert guids["imdb"] == "tt1234567"


def test_guid_extraction_falls_back_to_the_legacy_attribute():
    element = ET.fromstring('<Directory ratingKey="2" title="Y" guid="com.plexapp.agents.themoviedb://55?lang=en"/>')
    assert _extract_guids(element)["tmdb"] == "55"


def test_item_uid_prefers_tmdb_and_is_stable():
    a = PlexItem(rating_key="1", title="X", type="show", section="Shows", tmdb_id=42)
    b = PlexItem(rating_key="9999", title="X", type="show", section="Shows", tmdb_id=42)
    assert a.uid == b.uid == "tmdb:show:42"


def test_item_uid_falls_back_when_there_is_no_guid():
    """The fallback is server-agnostic - the same item is not 'plex:' on Plex
    and 'jellyfin:' on Jellyfin, or a manual assignment would not survive a move."""
    item = PlexItem(rating_key="7", title="X", type="show", section="Shows")
    assert item.uid == "local:Shows:7"


# -- fakes ---------------------------------------------------------------

FAKE_LIBRARY = [
    # tmdb_id, title, year, network, genres, keywords, lang, country
    (101, "A Brand New HBO Drama", 2025, ["HBO"], ["Drama"], [], "en", ["US"]),
    (102, "A Brand New Netflix Comedy", 2024, ["Netflix"], ["Comedy"], [], "en", ["US"]),
    (103, "A Peacock Original", 2023, ["Peacock"], ["Drama"], [], "en", ["US"]),
    (104, "Wandering The Globe", 2022, ["Unmapped Service"], ["Documentary"], ["travel"], "en", ["US"]),
    (105, "Mystery Meat", 2021, [], [], [], "en", ["US"]),
    (106, "An Unlisted Anime", 2020, ["Unmapped JP"], ["Animation"], [], "ja", ["JP"]),
    (107, "A Co Production", 2025, ["BBC One", "HBO"], ["Drama"], [], "en", ["GB"]),
]


class FakePlex:
    name = "plex"

    def __init__(self, *args, **kwargs):
        pass

    async def fetch_library(self, wanted=None, types=("show",)):
        items = [
            PlexItem(
                rating_key=str(tmdb_id),
                title=title,
                type="show",
                section="Shows",
                year=year,
                tmdb_id=tmdb_id,
                episode_count=10,
            )
            for tmdb_id, title, year, *_ in FAKE_LIBRARY
        ]
        return items, [PlexSection(key="1", title="Shows", type="show")]


class FakeTMDB:
    def __init__(self, *args, **kwargs):
        pass

    async def series(self, ids, progress=None):
        out = {}
        for tmdb_id, title, year, networks, genres, keywords, lang, country in FAKE_LIBRARY:
            if tmdb_id in ids:
                out[tmdb_id] = TMDBSeries(
                    tmdb_id=tmdb_id,
                    name=title,
                    networks=networks,
                    genres=genres,
                    keywords=keywords,
                    original_language=lang,
                    origin_country=country,
                    first_air_date=f"{year}-01-01",
                    episode_count=10,
                )
        return out

    async def movies(self, ids, progress=None):
        return {}


@pytest.fixture
def cfg(tmp_path):
    return Config(
        plex=PlexConfig(url="http://fake:32400", token="t"),
        tmdb=TMDBConfig(api_key="k"),
        data=DataConfig(
            channels_csv=str(DATA / "channels.csv"),
            network_map=str(DATA / "network_map.csv"),
            orphan_networks=str(DATA / "orphan_networks.csv"),
            channel_catalog=str(DATA / "channel_catalog.csv"),
            cache_dir=str(tmp_path / "cache"),
            state_file=str(tmp_path / "state.json"),
        ),
        root=tmp_path,
    )


@pytest.fixture
def scan(monkeypatch, cfg, catalog, defaults):
    monkeypatch.setattr("nostalgia_line.pipeline.build_source", lambda cfg: FakePlex())
    monkeypatch.setattr("nostalgia_line.pipeline.TMDBClient", FakeTMDB)

    def go(stations=None, overrides=None, **kwargs):
        return asyncio.run(
            run_scan(cfg, catalog, defaults, stations or StationBook(), overrides=overrides, **kwargs)
        )

    return go


# -- the pipeline --------------------------------------------------------


def test_scan_places_every_title_or_flags_it(scan):
    result = scan()
    assert len(result.entries) == len(FAKE_LIBRARY)
    for entry in result.entries:
        placed = entry.status in (STATUS_APP, STATUS_LINE)
        assert placed or entry.resolution.needs_review, f"{entry.title} vanished silently"


def test_scan_routes_by_network(scan):
    result = scan()
    by_title = {e.title: e for e in result.entries}
    assert by_title["A Brand New HBO Drama"].channels == [1068]
    assert by_title["A Brand New Netflix Comedy"].channels == [1064]


def test_scan_flags_the_orphan_network(scan):
    result = scan()
    peacock = next(e for e in result.entries if e.title == "A Peacock Original")
    assert peacock.channels == [1018]
    assert peacock.resolution.needs_review


def test_scan_finds_travel_by_keyword(scan):
    result = scan()
    travel = next(e for e in result.entries if e.title == "Wandering The Globe")
    assert travel.channels == [1059]


def test_scan_leaves_the_hopeless_case_unassigned(scan):
    result = scan()
    mystery = next(e for e in result.entries if e.title == "Mystery Meat")
    assert mystery.status == STATUS_UNASSIGNED
    assert mystery.resolution.needs_review


def test_scan_emits_the_coproduction_on_both_channels(scan):
    result = scan()
    coprod = next(e for e in result.entries if e.title == "A Co Production")
    assert set(coprod.channels) == {1026, 1068}


def test_stats_add_up(scan):
    result = scan()
    stats = result.stats()
    assert stats["total"] == len(FAKE_LIBRARY)
    assert (
        stats["already_assigned"] + stats["assigned_by_line"] + stats["unassigned"]
        == stats["total"]
    )
    assert 0 <= stats["coverage_pct"] <= 100


def test_channel_rollup_flags_empty_channels(scan, catalog, defaults):
    result = scan()
    rollup = result.channel_rollup(catalog, defaults)
    assert len(rollup) == len(catalog)
    hbo = next(r for r in rollup if r["number"] == 1068)
    assert hbo["added"] >= 1
    assert hbo["total"] == hbo["existing"] + hbo["added"]
    # music channels hold nothing and must not be reported as a problem to fix
    tune = next(r for r in rollup if r["number"] == 1074)
    assert tune["empty"] is False


def test_review_queue_holds_only_flagged_items(scan):
    result = scan()
    queue = result.review_queue()
    assert queue
    assert all(e.resolution.needs_review for e in queue)


# -- custom stations end to end ------------------------------------------


def test_custom_station_claims_content_through_the_pipeline(scan, catalog):
    book = StationBook([CustomStation(number=1300, name="My Prestige", source_channels=[1068])])
    book.register_with(catalog)
    result = scan(stations=book)
    drama = next(e for e in result.entries if e.title == "A Brand New HBO Drama")
    assert drama.channels == [1300]
    catalog.remove(1300)


def test_custom_station_can_adopt_an_unmapped_network(scan, catalog):
    book = StationBook([CustomStation(number=1301, name="Odd Service", source_networks=["Unmapped Service"])])
    book.register_with(catalog)
    result = scan(stations=book)
    travel = next(e for e in result.entries if e.title == "Wandering The Globe")
    assert travel.channels == [1301]
    catalog.remove(1301)


# -- overrides persist ---------------------------------------------------


def test_override_replaces_the_cascade_result(scan, catalog):
    result = scan()
    entry = next(e for e in result.entries if e.title == "Mystery Meat")
    apply_override(entry, [1044], catalog)
    assert entry.status == STATUS_LINE
    assert entry.channels == [1044]
    assert entry.overridden
    assert not entry.resolution.needs_review


def test_override_to_nothing_marks_it_unassigned(scan, catalog):
    result = scan()
    entry = next(e for e in result.entries if e.title == "A Brand New HBO Drama")
    apply_override(entry, [], catalog)
    assert entry.status == STATUS_UNASSIGNED
    assert entry.channels == []


def test_overrides_are_applied_during_a_scan(scan):
    result = scan(overrides={"tmdb:show:105": [1044]})
    entry = next(e for e in result.entries if e.title == "Mystery Meat")
    assert entry.channels == [1044]
    assert entry.overridden


def test_store_round_trips(tmp_path):
    path = tmp_path / "state.json"
    store = Store(path)
    store.set_override("tmdb:show:1", [1068, 1064])
    store.dismiss("tmdb:show:2")

    reloaded = Store(path)
    assert reloaded.overrides == {"tmdb:show:1": [1064, 1068]}
    assert reloaded.dismissed == {"tmdb:show:2"}


def test_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = Store(path)
    assert store.overrides == {}


# -- delta between scans (HANDOVER item 2) --------------------------------


def test_delta_marks_new_changed_and_unchanged(scan, catalog):
    from nostalgia_line.pipeline import apply_delta

    previous = scan()
    current = scan()

    # One title moved (simulate a remap by overriding the earlier scan)...
    moved = next(e for e in previous.entries if e.title == "A Brand New HBO Drama")
    apply_override(moved, [1044], catalog)
    # ...and one title vanished from the library since.
    departed = next(e for e in previous.entries if e.title == "Mystery Meat")
    previous.entries.remove(departed)

    apply_delta(current, previous)
    by_title = {e.title: e for e in current.entries}
    assert by_title["A Brand New HBO Drama"].delta == "changed"
    assert by_title["Mystery Meat"].delta == "new"
    assert by_title["A Brand New Netflix Comedy"].delta == "unchanged"
    assert [d["title"] for d in current.departed] == []
    assert current.previous_scan_at == previous.finished_at


def test_delta_reports_departed_titles(scan):
    from nostalgia_line.pipeline import apply_delta

    previous = scan()
    current = scan()
    current.entries = [e for e in current.entries if e.title != "Mystery Meat"]
    apply_delta(current, previous)
    assert [d["title"] for d in current.departed] == ["Mystery Meat"]


def test_the_first_scan_claims_no_delta(scan):
    """806 titles marked 'new' on the first import would be noise, not signal."""
    from nostalgia_line.pipeline import apply_delta

    result = scan()
    apply_delta(result, None)
    assert all(e.delta == "" for e in result.entries)
    assert result.previous_scan_at == 0.0
    assert result.stats()["delta"]["tracked"] is False


def test_delta_is_keyed_on_uid_alone(scan):
    """uid survives a re-scan and a Plex->Jellyfin move; nothing else does.
    A renamed title with the same uid is the same entry, not a new one."""
    from nostalgia_line.pipeline import apply_delta

    previous = scan()
    current = scan()
    renamed = next(e for e in current.entries if e.title == "A Brand New HBO Drama")
    renamed.title = "An HBO Drama, Renamed"
    apply_delta(current, previous)
    assert renamed.delta == "unchanged"


def test_the_delta_survives_a_restart(scan, tmp_path):
    """Spec'd in the handover: restarting the container must not lose the
    delta. It is persisted with the scan, so loading the scan restores it."""
    from nostalgia_line.pipeline import apply_delta

    previous = scan()
    current = scan()
    current.entries[0].title = "__wholly_new__"
    current.entries[0].uid = "tmdb:show:99999"
    apply_delta(current, previous)
    assert current.entries[0].delta == "new"
    departed_before = [d["uid"] for d in current.departed]

    path = tmp_path / "scan.json.gz"
    current.save(path)
    reloaded = ScanResult.load(path)

    assert reloaded is not None
    assert reloaded.entries[0].delta == "new"
    assert reloaded.previous_scan_at == previous.finished_at
    assert [d["uid"] for d in reloaded.departed] == departed_before
    assert reloaded.stats()["since_last_scan"] == current.stats()["since_last_scan"]


def test_stats_count_the_delta(scan):
    from nostalgia_line.pipeline import apply_delta

    previous = scan()
    current = scan()
    current.entries[0].uid = "tmdb:show:77777"
    apply_delta(current, previous)
    stats = current.stats()
    assert stats["delta"]["tracked"] is True
    assert stats["delta"]["new"] == 1
    assert stats["delta"]["departed"] == 1
    assert stats["since_last_scan"] == stats["delta"]["new"] + stats["delta"]["changed"]
