"""Catalog, normalization and default-assignment behaviour (spec S4, S6, S7)."""
from nostalgia_line.channels import normalize_title, strip_year


def test_catalog_has_113_channels(catalog):
    assert len(catalog) == 113
    assert catalog.require(1001).name == "Dizzy Channel"
    assert catalog.require(1113).name == "Oscar Hits"


def test_lackluster_sits_at_1054(catalog):
    """Spec S4: the numbering gotcha. Off-by-one here shifts every channel above it."""
    assert catalog.require(1053).name == "VHS Channel"
    assert catalog.require(1054).name == "LACKLUSTER"
    assert catalog.require(1055).name == "SPARZ"


def test_music_and_utility_channels_take_no_content(catalog):
    """Spec S4: 1072-1088 exist but never receive routed content."""
    for number in range(1072, 1089):
        assert catalog.require(number).accepts_content is False
    assert catalog.require(1071).accepts_content is True
    assert catalog.require(1089).accepts_content is True


def test_routable_excludes_the_no_content_band(catalog):
    numbers = {c.number for c in catalog.routable()}
    assert numbers.isdisjoint(range(1072, 1089))
    assert len(numbers) == 113 - 17


def test_strip_year_handles_plex_disambiguation():
    assert strip_year("Our Planet (2019)") == ("Our Planet", 2019)
    assert strip_year("Rugrats (2021)") == ("Rugrats", 2021)
    assert strip_year("Cosmos") == ("Cosmos", None)


def test_normalize_title_is_stable_across_plex_quirks():
    """Spec S7: strip trailing year, leading article, case and punctuation."""
    assert normalize_title("The Office (2005)") == "office"
    assert normalize_title("the office") == "office"
    assert normalize_title("Marvel's Agents of S.H.I.E.L.D.") == "marvelsagentsofshield"
    assert normalize_title("Bob's Burgers") == normalize_title("bobs burgers")


def test_normalize_does_not_collapse_distinct_titles():
    assert normalize_title("Scrubs") != normalize_title("Scrub")


def test_defaults_load_and_carry_expected_shape(defaults):
    assert len(defaults) == 4651
    channels = {row.channel_number for row in defaults.rows}
    assert channels <= set(range(1001, 1114))


def test_same_title_collisions_are_kept_apart(defaults):
    """Spec S7: Aladdin the 1992 film and the 1994 series are different rows."""
    aladdins = [r for r in defaults.rows if r.title.casefold() == "aladdin"]
    assert len(aladdins) >= 2
    years = {r.release_year for r in aladdins}
    assert len(years) >= 2, "expected Aladdin to appear with more than one year"


def test_lookup_prefers_exact_year(defaults):
    aladdins = [r for r in defaults.rows if r.title.casefold() == "aladdin"]
    target = aladdins[0]
    found = defaults.lookup("Aladdin", target.release_year)
    assert found
    assert all(r.release_year == target.release_year for r in found)


def test_lookup_refuses_to_guess_between_collisions(defaults):
    """A year that matches neither collision must not silently pick one."""
    aladdins = [r for r in defaults.rows if r.title.casefold() == "aladdin"]
    if len({r.release_year for r in aladdins}) < 2:
        return
    assert defaults.lookup("Aladdin", 1777) == []


def test_unknown_title_is_not_found(defaults):
    assert defaults.lookup("A Show That Does Not Exist Anywhere", 2024) == []


def test_sanctioned_pairs_match_the_spec_measurements(defaults):
    """Spec S6 measured 8.7% multi-channel and 226 sanctioned pairings."""
    stats = defaults.multi_channel_stats()
    assert stats["multi_channel_titles"] > 0
    ratio = stats["multi_channel_titles"] / stats["titles"]
    assert 0.05 < ratio < 0.15, f"multi-channel share drifted: {ratio:.3f}"
    assert stats["exactly_two"] > stats["three_or_more"]


def test_known_sibling_pairing_is_sanctioned(defaults):
    """Boomer-Rang + Cartoon Net is the single most common pairing in the file."""
    assert defaults.is_sanctioned_pair(1007, 1006)
    assert defaults.is_sanctioned_pair(1006, 1007)


def test_absurd_pairing_is_not_sanctioned(defaults):
    assert not defaults.is_sanctioned_pair(1001, 1060)


def test_network_map_covers_the_major_networks(network_map):
    for network in ("HBO", "Netflix", "NBC", "BBC One", "Cartoon Network"):
        assert network in network_map, f"{network} missing from network_map.csv"
    assert network_map.get("HBO")[0] == 1068
    assert network_map.get("Netflix")[0] == 1064


def test_network_map_targets_exist_in_the_catalog(network_map, catalog):
    """Every row, including the country-qualified ones, must point somewhere real."""
    for network, country, number, name in network_map.rows():
        channel = catalog.get(number)
        label = f"{network}{f' [{country}]' if country else ''}"
        assert channel is not None, f"{label} -> unknown channel {number}"
        assert channel.name == name, f"{label}: csv says {name}, catalog says {channel.name}"
        assert channel.accepts_content, f"{label} routes to no-content channel {name}"


def test_every_network_resolves_to_something(network_map):
    for network in network_map.names():
        assert network_map.get(network) is not None, f"{network} resolves to nothing"


def test_network_name_collisions_are_split_by_country(network_map):
    """TMDB lists a US TBS and a Japanese TBS. They must not share a channel."""
    assert network_map.get("TBS", ["US"])[0] == 1044
    assert network_map.get("TBS", ["JP"])[0] == 1071
    # No country information at all falls back to the unqualified row.
    assert network_map.get("TBS", [])[0] == 1044
    assert network_map.get("TBS")[0] == 1044


def test_unqualified_lookup_still_works_for_ordinary_networks(network_map):
    assert network_map.get("HBO", ["US"])[0] == 1068
    assert network_map.get("HBO", ["GB"])[0] == 1068


def test_unknown_network_returns_none(network_map):
    assert network_map.get("Not A Real Network") is None
    assert network_map.get(None) is None


def test_orphan_map_targets_exist_and_accept_content(orphan_map, catalog):
    for network, (number, name, _) in orphan_map.items():
        channel = catalog.get(number)
        assert channel is not None, f"orphan {network} -> unknown channel {number}"
        assert channel.name == name
        assert channel.accepts_content


def test_overlapping_orphan_and_network_entries_agree(orphan_map, network_map):
    """Shudder and Investigation Discovery appear in both tables.

    That is fine - the network map wins and they resolve at high confidence
    instead of via the orphan fallback - but the two tables must not disagree
    about where the content goes.
    """
    for network in set(orphan_map) & set(network_map.names()):
        mapped = network_map.get(network)
        assert orphan_map[network][0] == mapped[0], (
            f"{network}: network_map says {mapped[0]}, "
            f"orphan table says {orphan_map[network][0]}"
        )
