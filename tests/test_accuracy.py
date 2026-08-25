"""The accuracy probe (HANDOVER item 1).

The measurement compares the cascade with the lineup's own placements, so the
fixtures build a small lineup whose right answers are known, then hand the
probe TMDB records that agree or disagree on purpose.

The probe-prefix tests are the ones that matter: a regression there makes the
measurement compare the lineup with itself and report perfect agreement,
which looks exactly like good news.
"""
import pytest

from nostalgia_line import accuracy
from nostalgia_line.accuracy import MIN_SAMPLES, PROBE_PREFIX, measure
from nostalgia_line.cascade import STATUS_APP, STATUS_LINE, Cascade, Resolution
from nostalgia_line.channels import DefaultAssignments, DefaultRow
from nostalgia_line.pipeline import LibraryEntry, ScanResult
from nostalgia_line.stations import StationBook
from nostalgia_line.tmdb import TMDBCache, TMDBSeries

# One lineup row per case the probe must handle. The channel is the lineup's
# answer; the TMDB record below decides what the cascade will answer.
LINEUP = [
    (1068, "H.B.Yo Min", "Alpha Drama", 2020),    # HBO record       -> agree
    (1044, "TeeBS", "Beta Comedy", 2019),         # HBO record       -> disagree
    (1059, "Trip Channel", "Epsilon Trek", 2017), # travel keywords  -> agree (content rule)
    (1018, "N.B.Sea", "Zeta Original", 2021),     # Peacock          -> agree (orphan rule)
    (1099, "Spotlight", "Gamma Serial", 2018),    # genre suggestion -> suggestion agrees
    (1064, "Netflicks", "Delta Reruns", 2016),    # genre suggestion -> suggestion disagrees
    (1026, "A.B.Sea", "Eta Cold Case", 2015),     # no cached record -> skipped
    (1026, "A.B.Sea", "Theta Untagged", 2014),    # no tmdb id       -> skipped
]

RECORDS = {
    101: TMDBSeries(tmdb_id=101, networks=["HBO"], first_air_date="2020-01-01"),
    102: TMDBSeries(tmdb_id=102, networks=["HBO"], first_air_date="2019-01-01"),
    103: TMDBSeries(tmdb_id=103, networks=["Nowhere Stream"], keywords=["travel"]),
    104: TMDBSeries(tmdb_id=104, networks=["Peacock"]),
    105: TMDBSeries(tmdb_id=105, networks=[], genres=["Drama"]),
    106: TMDBSeries(tmdb_id=106, networks=[], genres=["Drama"]),
    # 107 deliberately absent from the cache; 108 has no tmdb id at all.
}

TMDB_IDS = {
    "Alpha Drama": 101, "Beta Comedy": 102, "Epsilon Trek": 103, "Zeta Original": 104,
    "Gamma Serial": 105, "Delta Reruns": 106, "Eta Cold Case": 107, "Theta Untagged": None,
}


@pytest.fixture()
def lineup() -> DefaultAssignments:
    return DefaultAssignments(
        [DefaultRow(number, name, title, year, str(year)) for number, name, title, year in LINEUP]
    )


@pytest.fixture()
def probe_cascade(catalog, network_map, orphan_map, lineup) -> Cascade:
    return Cascade(
        catalog=catalog,
        defaults=lineup,
        network_map=network_map,
        orphan_map=orphan_map,
        stations=StationBook(),
    )


@pytest.fixture()
def ground_truth() -> ScanResult:
    entries = [
        LibraryEntry(
            uid=f"tmdb:show:{TMDB_IDS[title] or 900 + number}",
            title=title,
            year=year,
            type="show",
            section="Shows",
            episode_count=10,
            tmdb_id=TMDB_IDS[title],
            resolution=Resolution(status=STATUS_APP, existing_channels=[number]),
            network=None,
        )
        for number, _, title, year in LINEUP
    ]
    return ScanResult(entries=entries)


@pytest.fixture()
def cache(tmp_path) -> TMDBCache:
    store = TMDBCache(tmp_path / "cache")
    for tmdb_id, record in RECORDS.items():
        store.put("series", tmdb_id, record.to_dict())
    return store


# -- the probe prefix ----------------------------------------------------


def test_a_lineup_title_short_circuits_without_the_prefix(probe_cascade):
    """Step 0 answers for any title already in the lineup - that is its job."""
    resolution = probe_cascade.resolve_series("Alpha Drama", 2020, RECORDS[101])
    assert resolution.status == STATUS_APP
    assert resolution.existing_channels == [1068]


def test_the_prefix_forces_an_independent_opinion(probe_cascade):
    resolution = probe_cascade.resolve_series(
        PROBE_PREFIX + "Alpha Drama", 2020, RECORDS[101]
    )
    assert resolution.status == STATUS_LINE
    assert resolution.primary.channel_number == 1068
    assert resolution.primary.rule == "network"


def test_without_the_prefix_the_measurement_collapses(
    monkeypatch, ground_truth, probe_cascade, cache
):
    """The regression this module must never suffer: probing under the real
    title makes every probe short-circuit to the existing answer, the sample
    count silently drops to zero, and 'no disagreements' reads as perfect
    agreement."""
    monkeypatch.setattr(accuracy, "PROBE_PREFIX", "")
    report = measure(ground_truth, probe_cascade, cache)
    assert report.sampled == 0
    assert report.skipped["no_opinion"] == report.ground_truth - 2  # the two skips

    # And the guard: with the real prefix the same inputs produce samples.
    monkeypatch.setattr(accuracy, "PROBE_PREFIX", PROBE_PREFIX)
    report = measure(ground_truth, probe_cascade, cache)
    assert report.sampled > 0


# -- the measurement -----------------------------------------------------


def test_measure_scores_agreement_per_rule(ground_truth, probe_cascade, cache):
    report = measure(ground_truth, probe_cascade, cache)

    assert report.ground_truth == len(LINEUP)
    assert report.skipped == {"no_tmdb_id": 1, "no_cached_record": 1, "no_opinion": 2}
    # Alpha, Beta, Epsilon, Zeta produced opinions; Gamma and Delta only suggest.
    assert report.sampled == 4
    assert report.agree == 3
    assert report.by_rule["network"] == [1, 2]
    assert report.by_rule["content_type"] == [1, 1]
    assert report.by_rule["orphan_network"] == [1, 1]


def test_the_disagreement_names_both_sides(ground_truth, probe_cascade, cache):
    report = measure(ground_truth, probe_cascade, cache)
    assert len(report.disagreements) == 1
    miss = report.disagreements[0]
    assert miss.title == "Beta Comedy"
    assert miss.rule == "network"
    assert {c["number"] for c in miss.ours} == {1068}
    assert miss.theirs == [{"number": 1044, "name": "TeeBS"}]


def test_the_retired_genre_rule_is_scored_on_its_suggestions(
    ground_truth, probe_cascade, cache
):
    """Genre no longer places anything, but its track record is the evidence
    the films decision will need - so keep measuring what it would have done."""
    report = measure(ground_truth, probe_cascade, cache)
    assert report.suggestion_n == 2
    assert report.suggestion_agree == 1  # Gamma agrees, Delta does not


def test_movies_are_not_probed_against_the_series_cache(probe_cascade, cache):
    """A movie's tmdb id can collide with an unrelated series id, so film rows
    must never be scored through the series cache."""
    result = ScanResult(
        entries=[
            LibraryEntry(
                uid="tmdb:movie:101",
                title="Alpha Drama",
                year=2020,
                type="movie",
                section="Films",
                episode_count=0,
                tmdb_id=101,
                resolution=Resolution(status=STATUS_APP, existing_channels=[1068]),
            )
        ]
    )
    report = measure(result, probe_cascade, cache)
    assert report.ground_truth == 0
    assert report.sampled == 0


def test_small_samples_render_no_verdict(ground_truth, probe_cascade, cache):
    """0/9 is a signal, not a proof. Below MIN_SAMPLES the payload says the
    count is insufficient rather than pretending a percentage is a verdict."""
    report = measure(ground_truth, probe_cascade, cache).to_dict()
    assert report["sampled"] < MIN_SAMPLES
    assert report["sufficient"] is False
    assert all(row["sufficient"] is False for row in report["by_rule"])
    assert all(row["n"] >= 1 for row in report["by_rule"]), "n reported beside every rate"


def test_modes_measure_differently_on_the_same_ground_truth(
    catalog, network_map, orphan_map, lineup, ground_truth, cache
):
    """Themed mode skips the network step entirely - the same library must
    yield a different (and here smaller) sample, which is exactly what the
    mode comparison exists to show."""
    def cascade_for(mode):
        return Cascade(
            catalog=catalog, defaults=lineup, network_map=network_map,
            orphan_map=orphan_map, stations=StationBook(), mode=mode,
        )

    streaming = measure(ground_truth, cascade_for("streaming_first"), cache)
    themed = measure(ground_truth, cascade_for("themed"), cache)
    assert "network" in streaming.by_rule
    assert "network" not in themed.by_rule
    assert themed.sampled < streaming.sampled


def test_an_unplaceable_title_counts_as_no_opinion_not_disagreement(
    probe_cascade, cache
):
    result = ScanResult(
        entries=[
            LibraryEntry(
                uid="tmdb:show:333",
                title="Un-Routable",
                year=2020,
                type="show",
                section="Shows",
                episode_count=1,
                tmdb_id=333,
                resolution=Resolution(status=STATUS_APP, existing_channels=[1068]),
            )
        ]
    )
    cache.put("series", 333, TMDBSeries(tmdb_id=333, networks=[], genres=[]).to_dict())
    report = measure(result, probe_cascade, cache)
    assert report.skipped["no_opinion"] == 1
    assert report.sampled == 0
    assert report.disagreements == []
