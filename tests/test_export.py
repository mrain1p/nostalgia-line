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

    original_keys = {r.key() for r in defaults.rows}
    written_keys = {(int(r[0]), r[2], r[3]) for r in written}
    assert original_keys <= written_keys, "an original row was lost"


def test_exported_additions_reimport_cleanly(result, catalog, defaults, tmp_path):
    """The merged file must parse back through the same loader NostalgiaTV expects."""
    from nostalgia_line.channels import DefaultAssignments

    additions = tmp_path / "a.csv"
    merged = tmp_path / "m.csv"
    export(result, catalog, defaults, additions, merged)
    reloaded = DefaultAssignments.load(merged)
    assert len(reloaded) == len(defaults) + 2
    assert reloaded.channels_for("New HBO Show", 2024) == {1068}
