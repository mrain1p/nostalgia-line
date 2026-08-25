"""CSV export and the additive-only integrity guarantee (spec S7)."""
import csv

import pytest

from nostalgia_line.cascade import HIGH, LOW, STATUS_APP, STATUS_LINE, Assignment, Resolution
from nostalgia_line.channels import DefaultRow
from nostalgia_line.export import (
    HEADER,
    IntegrityError,
    assert_additive,
    build_addition_rows,
    export,
)
from nostalgia_line.pipeline import LibraryEntry, ScanResult


def entry(uid, title, year, assignments, status=STATUS_LINE, review=False, overridden=False):
    return LibraryEntry(
        uid=uid,
        title=title,
        year=year,
        type="show",
        section="Shows",
        episode_count=10,
        tmdb_id=int(uid.split(":")[-1]) if uid.split(":")[-1].isdigit() else None,
        resolution=Resolution(status=status, assignments=assignments, needs_review=review),
        overridden=overridden,
    )


def assignment(number, name, primary=True, confidence=HIGH):
    return Assignment(number, name, "network", confidence, "test", primary=primary)


@pytest.fixture
def result():
    return ScanResult(
        entries=[
            entry("tmdb:show:1", "New HBO Show", 2024, [assignment(1068, "H.B.Yo Min")]),
            entry("tmdb:show:2", "New Netflix Show", 2023, [assignment(1064, "Netflicks")]),
        ],
        sections=["Shows"],
    )


def test_additions_contain_only_new_rows(result, catalog, defaults):
    rows, secondary, skipped = build_addition_rows(result, catalog, defaults)
    assert len(rows) == 2
    assert secondary == 0
    assert skipped == 0
    titles = {r.title for r in rows}
    assert titles == {"New HBO Show", "New Netflix Show"}


def test_review_items_are_skipped_by_default(catalog, defaults):
    result = ScanResult(
        entries=[
            entry("tmdb:show:3", "Uncertain Show", 2024, [assignment(1099, "Spotlight", confidence=LOW)], review=True)
        ]
    )
    rows, _, skipped = build_addition_rows(result, catalog, defaults)
    assert rows == []
    assert skipped == 1

    rows, _, _ = build_addition_rows(result, catalog, defaults, include_review=True)
    assert len(rows) == 1


def test_hand_reviewed_items_export_even_though_flagged(catalog, defaults):
    """An override is a human decision; it should not stay stuck behind the queue."""
    result = ScanResult(
        entries=[
            entry(
                "tmdb:show:4",
                "Hand Placed Show",
                2024,
                [assignment(1068, "H.B.Yo Min")],
                review=True,
                overridden=True,
            )
        ]
    )
    rows, _, skipped = build_addition_rows(result, catalog, defaults)
    assert len(rows) == 1
    assert skipped == 0


def test_already_assigned_items_produce_no_rows(catalog, defaults):
    result = ScanResult(entries=[entry("tmdb:show:5", "Old Show", 2001, [], status=STATUS_APP)])
    rows, _, _ = build_addition_rows(result, catalog, defaults)
    assert rows == []


def test_no_content_channels_are_never_written(catalog, defaults):
    """Spec S4: 1072-1088 must not appear in an export even if something targets them."""
    result = ScanResult(
        entries=[entry("tmdb:show:6", "Music Thing", 2020, [assignment(1074, "Tune Rock")])]
    )
    rows, _, _ = build_addition_rows(result, catalog, defaults)
    assert rows == []


def test_a_row_already_in_the_defaults_is_not_duplicated(catalog, defaults):
    existing = defaults.rows[0]
    result = ScanResult(
        entries=[
            entry(
                "tmdb:show:7",
                existing.title,
                existing.release_year,
                [assignment(existing.channel_number, existing.channel_name)],
            )
        ]
    )
    rows, _, _ = build_addition_rows(result, catalog, defaults)
    assert rows == []


def test_secondary_rows_are_counted(catalog, defaults):
    result = ScanResult(
        entries=[
            entry(
                "tmdb:show:8",
                "Co Production",
                2025,
                [
                    assignment(1026, "B.B.Sea"),
                    assignment(1068, "H.B.Yo Min", primary=False),
                ],
            )
        ]
    )
    rows, secondary, _ = build_addition_rows(result, catalog, defaults)
    assert len(rows) == 2
    assert secondary == 1


def test_assert_additive_accepts_a_superset():
    original = [DefaultRow(1068, "H.B.Yo Min", "A", 2020)]
    merged = original + [DefaultRow(1064, "Netflicks", "B", 2021)]
    assert_additive(original, merged)


def test_assert_additive_rejects_a_lost_row():
    original = [DefaultRow(1068, "H.B.Yo Min", "A", 2020)]
    assert_additive(original, list(original))
    with pytest.raises(IntegrityError):
        assert_additive(original, [DefaultRow(1064, "Netflicks", "B", 2021)])


def test_export_writes_both_files_and_preserves_every_original_row(result, catalog, defaults, tmp_path):
    additions = tmp_path / "channels_additions.csv"
    merged = tmp_path / "channels_merged.csv"
    report = export(result, catalog, defaults, additions, merged)

    assert report.additions == 2
    assert report.original_rows == len(defaults)
    assert report.merged_rows == len(defaults) + 2

    with open(merged, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        assert next(reader) == HEADER
        written = list(reader)
    assert len(written) == len(defaults) + 2

    # Compare the exact fields, not the dedupe key. The key folds an unparseable
    # year to "", so a row whose year said "Various" could be rewritten as blank
    # and still pass - which is exactly the bug this now guards.
    original_rows = {r.exact() for r in defaults.rows}
    written_rows = {tuple(r) for r in written}
    assert original_rows <= written_rows, "an original row was lost or altered"


def test_exported_additions_reimport_cleanly(result, catalog, defaults, tmp_path):
    """The merged file must parse back through the same loader NostalgiaTV expects."""
    from nostalgia_line.channels import DefaultAssignments

    additions = tmp_path / "a.csv"
    merged = tmp_path / "m.csv"
    export(result, catalog, defaults, additions, merged)
    reloaded = DefaultAssignments.load(merged)
    assert len(reloaded) == len(defaults) + 2
    assert reloaded.channels_for("New HBO Show", 2024) == {1068}


# -- byte-level fidelity to NostalgiaTV's own file -----------------------


def test_a_non_numeric_year_is_written_back_verbatim(catalog, defaults, tmp_path):
    """The stock file uses "Various" for compilation entries like Action Movies.
    Parsing the year to an int and writing it back destroyed those on 37 rows."""
    various = [r for r in defaults.rows if r.year_text and not r.year_text.isdigit()]
    assert various, "the stock file should contain non-numeric years"
    assert {r.year_text for r in various} == {"Various"}

    merged = tmp_path / "m.csv"
    export(ScanResult(entries=[]), catalog, defaults, tmp_path / "a.csv", merged)
    text = merged.read_text(encoding="utf-8")
    for row in various[:5]:
        assert f"{row.channel_number},{row.channel_name},{row.title},Various" in text


def test_the_merged_file_matches_their_line_endings_and_encoding(catalog, defaults, tmp_path):
    """Their export is plain UTF-8, bare LF, no BOM. csv.writer defaults to CRLF,
    which would make every single line differ from the file we were handed."""
    merged = tmp_path / "m.csv"
    export(ScanResult(entries=[]), catalog, defaults, tmp_path / "a.csv", merged)
    raw = merged.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "no BOM"
    assert b"\r\n" not in raw, "bare LF, not CRLF"
    assert raw.endswith(b"\n")


def test_a_round_trip_through_our_own_loader_is_byte_identical(catalog, defaults, tmp_path):
    """Export then re-import then export again must not drift."""
    first = tmp_path / "one.csv"
    export(ScanResult(entries=[]), catalog, defaults, tmp_path / "a.csv", first)

    from nostalgia_line.channels import DefaultAssignments

    second = tmp_path / "two.csv"
    export(ScanResult(entries=[]), catalog, DefaultAssignments.load(first), tmp_path / "b.csv", second)
    assert first.read_bytes() == second.read_bytes()


def test_awkward_titles_survive_a_round_trip(catalog, defaults, tmp_path):
    """Commas, quotes and non-ascii all appear in the real file."""
    from nostalgia_line.cascade import HIGH, Assignment, Resolution
    from nostalgia_line.channels import DefaultAssignments
    from nostalgia_line.pipeline import LibraryEntry

    awkward = ["Comma, In Title", 'Quote "Inside" It', "Pokémon Horizons", "WALL·E Redux"]
    entries = [
        LibraryEntry(
            uid=f"tmdb:show:{i}", title=t, year=2024, type="show", section="Shows",
            episode_count=1, tmdb_id=i,
            resolution=Resolution(
                status=STATUS_LINE,
                assignments=[Assignment(1068, "H.B.Yo Min", "network", HIGH, "test")],
            ),
        )
        for i, t in enumerate(awkward, start=1)
    ]
    merged = tmp_path / "m.csv"
    export(ScanResult(entries=entries), catalog, defaults, tmp_path / "a.csv", merged)

    reloaded = DefaultAssignments.load(merged)
    titles = {r.title for r in reloaded.rows}
    for t in awkward:
        assert t in titles, f"{t!r} did not survive the round trip"
