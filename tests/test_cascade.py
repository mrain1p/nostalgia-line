"""The resolution cascade (spec S3, S5, S6, S8, S10)."""
import pytest

from nostalgia_line.cascade import (
    HIGH,
    LOW,
    MEDIUM,
    STATUS_APP,
    STATUS_LINE,
    STATUS_UNASSIGNED,
    Cascade,
)
from nostalgia_line.stations import CustomStation, StationBook

from .conftest import series


# -- step 0: the defaults are authoritative ------------------------------


def test_existing_assignment_short_circuits_everything(cascade, defaults):
    """A title the app already places is reported as such, never re-routed."""
    row = defaults.rows[0]
    resolution = cascade.resolve_series(row.title, row.release_year, series(networks=["Netflix"]))
    assert resolution.status == STATUS_APP
    assert row.channel_number in resolution.existing_channels
    assert resolution.assignments == []


# -- step 1: network match -----------------------------------------------


def test_network_match_routes_to_the_parody_channel(cascade):
    resolution = cascade.resolve_series("DTF St. Louis", 2026, series(networks=["HBO"]))
    assert resolution.status == STATUS_LINE
    assert resolution.primary.channel_number == 1068
    assert resolution.primary.channel_name == "H.B.Yo Min"
    assert resolution.primary.confidence == HIGH
    assert not resolution.needs_review


def test_streaming_services_route_through_networks(cascade):
    """TMDB treats streaming services as TV networks (spec S2)."""
    for network, number in [
        ("Netflix", 1064),
        ("Apple TV+", 1065),
        ("Hulu", 1066),
        ("Prime Video", 1069),
        ("Disney+", 1063),
    ]:
        resolution = cascade.resolve_series(f"Unseen {network} Show", 2024, series(networks=[network]))
        assert resolution.primary.channel_number == number, network


def test_network_match_beats_a_misleading_title(cascade):
    """Spec S10: 'The Dark Wizard' reads as fantasy but is an HBO Max documentary."""
    resolution = cascade.resolve_series(
        "The Dark Wizard", 2024, series(networks=["HBO Max"], genres=["Documentary"])
    )
    assert resolution.primary.channel_number == 1068


# -- co-productions and multi-channel (spec S6) --------------------------


def test_coproduction_emits_both_networks(cascade):
    """Spec S6: 'Half Man' returns both BBC One and HBO. Emit both."""
    resolution = cascade.resolve_series("Half Man", 2025, series(networks=["BBC One", "HBO"]))
    numbers = [a.channel_number for a in resolution.assignments]
    assert numbers[0] == 1026
    assert 1068 in numbers
    assert resolution.assignments[0].primary is True
    assert resolution.assignments[1].primary is False


def test_multi_channel_off_suppresses_the_second_channel(catalog, defaults, network_map, orphan_map):
    cascade = Cascade(
        catalog, defaults, network_map, orphan_map, multi_channel="off"
    )
    resolution = cascade.resolve_series("Half Man", 2025, series(networks=["BBC One", "HBO"]))
    assert len(resolution.assignments) == 1


def test_unsanctioned_content_type_pairing_is_gated(cascade):
    """A content-type secondary only survives if the default file sanctions the pair."""
    resolution = cascade.resolve_series(
        "Some Cooking Thing", 2024, series(networks=["HBO"], keywords=["cooking"])
    )
    numbers = [a.channel_number for a in resolution.assignments]
    assert numbers[0] == 1068
    if len(numbers) > 1:
        assert cascade.defaults.is_sanctioned_pair(1068, numbers[1])


# -- step 2: orphan networks (spec S5) -----------------------------------


def test_orphan_network_falls_back_to_its_parent_and_flags_review(cascade):
    resolution = cascade.resolve_series("A Peacock Show", 2023, series(networks=["Peacock"]))
    assert resolution.primary.channel_number == 1018
    assert resolution.primary.confidence == MEDIUM
    assert resolution.needs_review, "orphan routing must always be reviewable"


def test_flag_only_mode_refuses_to_place_an_orphan(catalog, defaults, network_map, orphan_map):
    cascade = Cascade(
        catalog, defaults, network_map, orphan_map, orphan_policy="flag_only"
    )
    resolution = cascade.resolve_series("A Peacock Show", 2023, series(networks=["Peacock"]))
    assert resolution.status == STATUS_UNASSIGNED
    assert resolution.needs_review


def test_unlisted_orphan_does_not_land_on_a_generic_channel_unflagged(cascade):
    """Spec S5: this is how a Netflix comedy ends up on HGTV. It must be flagged."""
    resolution = cascade.resolve_series(
        "This Is a Gardening Show",
        2023,
        series(networks=["Some Unknown Service"], genres=["Comedy"]),
    )
    assert resolution.needs_review
    assert resolution.review_reason


# -- step 3: content-type rules (spec S3.3) ------------------------------


def test_travel_shows_are_found_by_keyword_not_genre(cascade):
    """Spec S1: TMDB's TV taxonomy has no Travel genre, so genres alone report zero.

    A synthetic title is used deliberately - the real travel shows the spec names
    are already in the shipped defaults and would short-circuit at step 0.
    """
    resolution = cascade.resolve_series(
        "A Wanderer Overseas",
        2010,
        series(networks=["Unmapped Net"], genres=["Documentary"], keywords=["travel"]),
    )
    assert resolution.primary.channel_number == 1059
    assert resolution.primary.channel_name == "Trip Channel"


def test_the_spec_travel_shows_are_already_placed(cascade):
    """The shows S1 says genre-routing loses are in fact in the default file."""
    for title, year in [("An Idiot Abroad", 2010)]:
        resolution = cascade.resolve_series(title, year, series(networks=["Unmapped"]))
        assert resolution.status == STATUS_APP, f"{title} should already be assigned"


def test_food_keywords_route_to_the_meal_network(cascade):
    resolution = cascade.resolve_series(
        "Some Feed Show", 2018, series(networks=["Unmapped Net"], keywords=["cooking"])
    )
    assert resolution.primary.channel_number == 1012


def test_true_crime_splits_on_genre(cascade):
    with_crime = cascade.resolve_series(
        "Case File", 2020, series(networks=["Unmapped"], genres=["Crime"], keywords=["true crime"])
    )
    without_crime = cascade.resolve_series(
        "Case File Two", 2020, series(networks=["Unmapped"], keywords=["true crime"])
    )
    assert with_crime.primary.channel_number == 1046
    assert without_crime.primary.channel_number == 1048


def test_nature_splits_on_documentary(cascade):
    doc = cascade.resolve_series(
        "Blue Deep", 2019, series(networks=["Unmapped"], genres=["Documentary"], keywords=["wildlife"])
    )
    non_doc = cascade.resolve_series(
        "Critter Time", 2019, series(networks=["Unmapped"], keywords=["wildlife"])
    )
    assert doc.primary.channel_number == 1035
    assert non_doc.primary.channel_number == 1033


def test_anime_is_detected_by_language_and_animation(cascade):
    resolution = cascade.resolve_series(
        "An Unlisted Shounen Series",
        2019,
        series(networks=["Unmapped"], genres=["Animation"], original_language="ja", origin_country=["JP"]),
    )
    assert resolution.primary.channel_number == 1071


def test_japanese_tbs_does_not_land_on_the_american_tbs(cascade):
    """TMDB lists a US TBS and a Japanese TBS. Name-only matching gets this wrong."""
    jp = cascade.resolve_series(
        "A Late Night Anime",
        2021,
        series(networks=["TBS"], genres=["Animation"], original_language="ja", origin_country=["JP"]),
    )
    us = cascade.resolve_series(
        "An American Sitcom Rerun",
        2011,
        series(networks=["TBS"], genres=["Comedy"], origin_country=["US"]),
    )
    assert jp.primary.channel_number == 1071, "Japanese TBS should reach Munchyroll"
    assert us.primary.channel_number == 1044, "American TBS should reach TeeBS"


def test_reality_with_no_other_signal_is_reviewed_not_buried(cascade):
    """There is no honest reality-TV channel, so it must not be invented."""
    resolution = cascade.resolve_series(
        "Some Unmapped Reality Show", 2019, series(networks=["Unmapped"], genres=["Reality"])
    )
    assert resolution.needs_review


def test_japanese_live_action_is_not_treated_as_anime(cascade):
    resolution = cascade.resolve_series(
        "Tokyo Drama",
        2019,
        series(networks=["Unmapped"], genres=["Drama"], original_language="ja", origin_country=["JP"]),
    )
    assert resolution.primary.channel_number != 1071


# -- step 4 and 5: genre fallback and unassigned -------------------------


def test_genre_fallback_is_low_confidence_and_always_reviewed(cascade):
    resolution = cascade.resolve_series(
        "Generic Drama", 2019, series(networks=["Unmapped"], genres=["Drama"])
    )
    assert resolution.primary.channel_number == 1099
    assert resolution.primary.confidence == LOW
    assert resolution.needs_review


def test_nothing_at_all_is_surfaced_not_dropped(cascade):
    """Spec S3.5: never silently drop."""
    resolution = cascade.resolve_series("Total Mystery", 2019, series(networks=[]))
    assert resolution.status == STATUS_UNASSIGNED
    assert resolution.needs_review
    assert resolution.review_reason


def test_missing_tmdb_record_is_flagged(cascade):
    resolution = cascade.resolve_series("No Guid Show", 2019, None)
    assert resolution.status == STATUS_UNASSIGNED
    assert resolution.needs_review
    assert "tmdb" in resolution.review_reason.lower()


def test_no_content_channels_are_never_a_routing_target(cascade, catalog):
    """Spec S4: 1072-1088 must never be produced by any rule."""
    samples = [
        series(networks=["HBO"]),
        series(networks=["Unmapped"], keywords=["travel"]),
        series(networks=["Unmapped"], genres=["Drama"]),
        series(networks=["Peacock"]),
        series(networks=[], genres=["Comedy"]),
    ]
    for record in samples:
        resolution = cascade.resolve_series("Probe", 2020, record)
        for assignment in resolution.assignments:
            channel = catalog.require(assignment.channel_number)
            assert channel.accepts_content, f"routed to no-content channel {channel.name}"


# -- routing modes (spec S8) ---------------------------------------------


def test_themed_mode_ignores_the_network_entirely(catalog, defaults, network_map, orphan_map):
    cascade = Cascade(catalog, defaults, network_map, orphan_map, mode="themed")
    resolution = cascade.resolve_series(
        "Travel Thing", 2020, series(networks=["HBO"], keywords=["travel"])
    )
    assert resolution.primary.channel_number == 1059


def test_streaming_first_keeps_the_network(cascade):
    resolution = cascade.resolve_series(
        "Travel Thing", 2020, series(networks=["HBO"], keywords=["travel"])
    )
    assert resolution.primary.channel_number == 1068


def test_hybrid_gives_content_type_the_first_claim(catalog, defaults, network_map, orphan_map):
    cascade = Cascade(catalog, defaults, network_map, orphan_map, mode="hybrid")
    resolution = cascade.resolve_series(
        "Travel Thing", 2020, series(networks=["HBO"], keywords=["travel"])
    )
    assert resolution.primary.channel_number == 1059


# -- custom stations ------------------------------------------------------


def test_custom_station_claims_its_configured_network(catalog, defaults, network_map, orphan_map):
    """'This station should use the lineup for G4.'"""
    book = StationBook([CustomStation(number=1200, name="Retro Gaming", source_networks=["G4"])])
    book.register_with(catalog)
    cascade = Cascade(catalog, defaults, network_map, orphan_map, stations=book)
    resolution = cascade.resolve_series("Attack of the Show", 2005, series(networks=["G4"]))
    assert resolution.primary.channel_number == 1200
    assert resolution.primary.channel_name == "Retro Gaming"
    assert resolution.primary.confidence == HIGH
    catalog.remove(1200)


def test_custom_station_can_claim_an_existing_channel_lineup(catalog, defaults, network_map, orphan_map):
    """'This station should use the lineup for Boomerang.'"""
    book = StationBook(
        [CustomStation(number=1201, name="Saturday Mornings", source_channels=[1007], mode="claim")]
    )
    book.register_with(catalog)
    cascade = Cascade(catalog, defaults, network_map, orphan_map, stations=book)
    resolution = cascade.resolve_series("Some Boomerang Show", 1998, series(networks=["Boomerang"]))
    numbers = [a.channel_number for a in resolution.assignments]
    assert 1201 in numbers
    assert 1007 not in numbers, "claim mode should replace the source channel"
    catalog.remove(1201)


def test_mirror_mode_keeps_both_channels(catalog, defaults, network_map, orphan_map):
    book = StationBook(
        [CustomStation(number=1202, name="Extra Toons", source_channels=[1007], mode="mirror")]
    )
    book.register_with(catalog)
    cascade = Cascade(catalog, defaults, network_map, orphan_map, stations=book)
    resolution = cascade.resolve_series("Some Boomerang Show", 1998, series(networks=["Boomerang"]))
    numbers = [a.channel_number for a in resolution.assignments]
    assert 1007 in numbers and 1202 in numbers
    catalog.remove(1202)


def test_station_with_no_sources_is_reported_as_a_problem(catalog):
    book = StationBook([CustomStation(number=1203, name="Empty")])
    problems = book.validate_against(catalog)
    assert any("no sources" in p for p in problems)


def test_station_colliding_with_a_stock_channel_is_reported(catalog):
    book = StationBook([CustomStation(number=1068, name="My HBO", source_networks=["G4"])])
    problems = book.validate_against(catalog)
    assert any("collides" in p for p in problems)


def test_station_rejects_a_bad_mode():
    with pytest.raises(ValueError):
        CustomStation(number=1204, name="Bad", mode="sideways")


# -- films (spec S3 "For films") ------------------------------------------


def movie(**kwargs):
    from nostalgia_line.tmdb import TMDBMovie

    kwargs.setdefault("tmdb_id", 1)
    kwargs.setdefault("title", "Test Film")
    kwargs.setdefault("release_date", "2015-01-01")
    return TMDBMovie(**kwargs)


def test_an_oscar_collection_wins_outright(cascade):
    resolution = cascade.resolve_movie(
        "Some Winner", 2015, movie(collection="Oscar Best Picture Collection")
    )
    assert resolution.primary.channel_number == 1113


def test_distinctive_genres_route_before_the_decade(cascade):
    for genre, channel in [("Western", 1100), ("War", 1103), ("Documentary", 1032)]:
        resolution = cascade.resolve_movie(f"A {genre} Film", 2015, movie(genres=[genre]))
        assert resolution.primary.channel_number == channel, genre


def test_the_era_split_keeps_modern_horror_from_swallowing_the_library(cascade):
    """Spec S3: without this, Terror Channel took 430 titles in testing."""
    old = cascade.resolve_movie("Old Fright", 1985, movie(genres=["Horror"], release_date="1985-01-01"))
    new = cascade.resolve_movie("New Fright", 2018, movie(genres=["Horror"], release_date="2018-01-01"))
    assert old.primary.channel_number == 1053, "pre-2000 horror goes to VHS Channel"
    assert new.primary.channel_number == 1037, "modern horror goes to Terror Channel"


def test_pre_1950_films_go_to_the_classic_channel(cascade):
    resolution = cascade.resolve_movie("Very Old", 1938, movie(release_date="1938-01-01"))
    assert resolution.primary.channel_number == 1050


def test_foreign_and_acclaimed_reaches_benchmark_hits(cascade):
    resolution = cascade.resolve_movie(
        "Un Film", 2010,
        movie(original_language="fr", vote_average=8.2, vote_count=2000, release_date="2010-01-01"),
    )
    assert resolution.primary.channel_number == 1112


def test_anime_films_are_excluded_from_benchmark_hits(cascade):
    """Spec S3.4: otherwise Demon Slayer lands alongside world cinema."""
    resolution = cascade.resolve_movie(
        "An Anime Film", 2020,
        movie(original_language="ja", genres=["Animation"], vote_average=8.5,
              vote_count=4000, release_date="2020-01-01"),
    )
    assert resolution.primary.channel_number != 1112


def test_the_decade_fallback_is_low_confidence_and_reviewed(cascade):
    resolution = cascade.resolve_movie(
        "Ordinary Film", 2003, movie(genres=["Comedy"], release_date="2003-01-01")
    )
    assert resolution.primary.channel_number == 1094, "The 2000's"
    assert resolution.primary.confidence == LOW
    assert resolution.needs_review


def test_a_film_already_in_the_lineup_is_left_alone(cascade, defaults):
    row = next(r for r in defaults.rows if r.release_year)
    resolution = cascade.resolve_movie(row.title, row.release_year, movie())
    assert resolution.status == STATUS_APP


def test_a_film_with_no_year_and_no_genre_is_surfaced(cascade):
    resolution = cascade.resolve_movie("Nothing Known", None, movie(release_date=""))
    assert resolution.status == STATUS_UNASSIGNED
    assert resolution.needs_review


def test_films_never_reach_a_no_content_channel(cascade, catalog):
    import random

    random.seed(11)
    genres = ["Drama", "Comedy", "Horror", "Western", "War", "Documentary", "Animation", "Music"]
    for i in range(200):
        year = random.randint(1930, 2025)
        resolution = cascade.resolve_movie(
            f"Probe {i}", year,
            movie(genres=random.sample(genres, k=1), release_date=f"{year}-01-01",
                  original_language=random.choice(["en", "ja", "fr"]),
                  vote_average=random.uniform(4, 9), vote_count=random.randint(0, 3000)),
        )
        for a in resolution.assignments:
            assert catalog.require(a.channel_number).accepts_content
