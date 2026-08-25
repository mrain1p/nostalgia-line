"""Network-level workflow: the rollup, user mappings, and bulk assignment.

One unmapped network can strand dozens of titles. These cover the machinery that
turns that into a single decision.
"""
import pytest

from nostalgia_line.cascade import Cascade, STATUS_LINE
from nostalgia_line.channels import load_network_map
from nostalgia_line.pipeline import LibraryEntry, ScanResult
from nostalgia_line.stations import StationBook

from .conftest import DATA, series


# -- the override layer on NetworkMap ------------------------------------


def test_override_beats_the_shipped_mapping(catalog):
    network_map = load_network_map(DATA / "network_map.csv")
    assert network_map.get("HBO")[0] == 1068
    network_map.set_override("HBO", 1044, "TeeBS")
    assert network_map.get("HBO")[0] == 1044
    assert network_map.is_overridden("HBO")


def test_override_beats_a_country_qualified_row(catalog):
    """A user decision outranks even the more specific shipped row."""
    network_map = load_network_map(DATA / "network_map.csv")
    assert network_map.get("TBS", ["JP"])[0] == 1071
    network_map.set_override("TBS", 1013, "TV World")
    assert network_map.get("TBS", ["JP"])[0] == 1013
    assert network_map.get("TBS", ["US"])[0] == 1013


def test_clearing_an_override_restores_the_shipped_mapping(catalog):
    network_map = load_network_map(DATA / "network_map.csv")
    network_map.set_override("HBO", 1044, "TeeBS")
    assert network_map.clear_override("HBO") is True
    assert network_map.get("HBO")[0] == 1068
    assert network_map.clear_override("HBO") is False


def test_override_can_map_a_network_the_file_never_heard_of(catalog):
    network_map = load_network_map(DATA / "network_map.csv")
    assert network_map.get("Adult Swim UK") is None
    network_map.set_override("Adult Swim UK", 1051, "Adult Skim")
    assert network_map.get("Adult Swim UK")[0] == 1051
    assert "Adult Swim UK" in network_map
    assert "adult swim uk" in [n.casefold() for n in network_map.names()]


def test_apply_overrides_ignores_unknown_channels(catalog):
    network_map = load_network_map(DATA / "network_map.csv")
    network_map.apply_overrides({"Some Net": 1068, "Bad Net": 4242}, catalog)
    assert network_map.get("Some Net")[0] == 1068
    assert network_map.get("Bad Net") is None


def test_apply_overrides_replaces_the_whole_layer(catalog):
    network_map = load_network_map(DATA / "network_map.csv")
    network_map.apply_overrides({"A Net": 1068}, catalog)
    network_map.apply_overrides({"B Net": 1064}, catalog)
    assert network_map.get("A Net") is None
    assert network_map.get("B Net")[0] == 1064


def test_a_mapped_network_routes_through_the_cascade(catalog, defaults, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    network_map.set_override("Adult Swim UK", 1051, "Adult Skim")
    cascade = Cascade(catalog, defaults, network_map, orphan_map, StationBook())
    resolution = cascade.resolve_series(
        "An Unlisted Late Night Cartoon", 2022, series(networks=["Adult Swim UK"])
    )
    assert resolution.status == STATUS_LINE
    assert resolution.primary.channel_number == 1051
    assert not resolution.needs_review, "a user's own mapping is not a guess"


# -- the rollup ----------------------------------------------------------


def entry(uid, title, network, status=STATUS_LINE, channels=(), review=False, countries=()):
    from nostalgia_line.cascade import HIGH, Assignment, Resolution

    return LibraryEntry(
        uid=uid,
        title=title,
        year=2020,
        type="show",
        section="Shows",
        episode_count=10,
        tmdb_id=int(uid.rsplit(":", 1)[-1]),
        network=network,
        origin_country=list(countries),
        resolution=Resolution(
            status=status,
            assignments=[
                Assignment(c, f"Ch{c}", "network", HIGH, "test") for c in channels
            ],
            network=network,
            needs_review=review,
        ),
    )


@pytest.fixture
def rollup_result():
    return ScanResult(
        entries=[
            entry("tmdb:show:1", "HBO One", "HBO", channels=[1068]),
            entry("tmdb:show:2", "HBO Two", "HBO", channels=[1068]),
            entry("tmdb:show:3", "Peacock One", "Peacock", channels=[1018], review=True),
            entry("tmdb:show:4", "Weird One", "Weird Service", channels=[1099], review=True),
            entry("tmdb:show:5", "Weird Two", "Weird Service", channels=[1099], review=True),
            entry("tmdb:show:6", "Weird Three", "Weird Service", channels=[1099], review=True),
        ]
    )


def test_rollup_groups_by_network(rollup_result, catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    rows = rollup_result.network_rollup(network_map, orphan_map, catalog)
    by_network = {r["network"]: r for r in rows}
    assert by_network["HBO"]["titles"] == 2
    assert by_network["Weird Service"]["titles"] == 3
    assert by_network["HBO"]["episodes"] == 20


def test_rollup_puts_the_biggest_unmapped_network_first(rollup_result, catalog, orphan_map):
    """The whole point: show the user their best use of attention."""
    network_map = load_network_map(DATA / "network_map.csv")
    rows = rollup_result.network_rollup(network_map, orphan_map, catalog)
    assert rows[0]["network"] == "Weird Service"
    assert rows[0]["status"] == "unmapped"
    assert rows[0]["titles"] == 3


def test_rollup_labels_each_status(rollup_result, catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    rows = rollup_result.network_rollup(network_map, orphan_map, catalog)
    status = {r["network"]: r["status"] for r in rows}
    assert status["HBO"] == "mapped"
    assert status["Peacock"] == "orphan"
    assert status["Weird Service"] == "unmapped"


def test_rollup_marks_a_user_mapping_as_custom(rollup_result, catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    network_map.set_override("Weird Service", 1099, "Spotlight")
    rows = rollup_result.network_rollup(network_map, orphan_map, catalog)
    weird = next(r for r in rows if r["network"] == "Weird Service")
    assert weird["status"] == "custom"
    assert weird["channel_number"] == 1099


def test_rollup_reports_where_titles_actually_landed(rollup_result, catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    rows = rollup_result.network_rollup(network_map, orphan_map, catalog)
    hbo = next(r for r in rows if r["network"] == "HBO")
    assert hbo["landing"] == [{"number": 1068, "name": "H.B.Yo Min", "titles": 2}]
    assert hbo["samples"] == ["HBO One", "HBO Two"]


def test_rollup_counts_review_and_unassigned(rollup_result, catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    rows = rollup_result.network_rollup(network_map, orphan_map, catalog)
    weird = next(r for r in rows if r["network"] == "Weird Service")
    assert weird["needs_review"] == 3
    assert next(r for r in rows if r["network"] == "HBO")["needs_review"] == 0


def test_rollup_uses_origin_country_for_collisions(catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    result = ScanResult(
        entries=[entry("tmdb:show:9", "Anime Thing", "TBS", channels=[1071], countries=["JP"])]
    )
    rows = result.network_rollup(network_map, orphan_map, catalog)
    assert rows[0]["channel_number"] == 1071, "Japanese TBS should report Munchyroll"


def test_titles_with_no_network_are_left_out_of_the_rollup(catalog, orphan_map):
    network_map = load_network_map(DATA / "network_map.csv")
    result = ScanResult(entries=[entry("tmdb:show:8", "Nameless", None, channels=[])])
    assert result.network_rollup(network_map, orphan_map, catalog) == []


# -- diagnostics ---------------------------------------------------------


def test_diagnostics_surface_items_with_no_tmdb_id():
    """The silent failure: Plex has no guid, so nothing can ever route."""
    missing = LibraryEntry(
        uid="plex:Shows:12",
        title="No Guid Show",
        year=2020,
        type="show",
        section="Shows",
        episode_count=4,
        tmdb_id=None,
        resolution=__import__(
            "nostalgia_line.cascade", fromlist=["Resolution"]
        ).Resolution(status="unassigned", needs_review=True),
    )
    result = ScanResult(entries=[missing, entry("tmdb:show:1", "Fine", "HBO", channels=[1068])])
    diagnostics = result.diagnostics()
    assert diagnostics["no_tmdb_id"] == 1
    assert diagnostics["no_tmdb_samples"] == ["No Guid Show"]


def test_diagnostics_report_shows_tmdb_knows_nothing_about():
    result = ScanResult(entries=[entry("tmdb:show:1", "Networkless", None, channels=[])])
    assert result.diagnostics()["no_network"] == 1
