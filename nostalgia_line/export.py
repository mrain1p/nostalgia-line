"""CSV export - strictly additive, with the integrity assertion from spec S7."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .cascade import STATUS_LINE
from .channels import ChannelCatalog, DefaultAssignments, DefaultRow
from .pipeline import ScanResult

HEADER = ["Channel Number", "Channel Name", "Title", "Release Year"]


class IntegrityError(RuntimeError):
    """The merged file would not contain every original row. Never write it."""


@dataclass
class ExportReport:
    additions_path: str
    merged_path: str
    additions: int
    merged_rows: int
    original_rows: int
    secondary_rows: int
    skipped_review: int

    def to_dict(self) -> dict:
        return {
            "additions_path": self.additions_path,
            "merged_path": self.merged_path,
            "additions": self.additions,
            "merged_rows": self.merged_rows,
            "original_rows": self.original_rows,
            "secondary_rows": self.secondary_rows,
            "skipped_review": self.skipped_review,
            "secondary_pct": (
                round(100.0 * self.secondary_rows / self.additions, 1) if self.additions else 0.0
            ),
        }


def build_addition_rows(
    result: ScanResult,
    catalog: ChannelCatalog,
    defaults: DefaultAssignments,
    include_review: bool = False,
) -> tuple[list[DefaultRow], int, int]:
    """Rows Nostalgia Line wants to add. Returns (rows, secondary_count, skipped)."""
    rows: list[DefaultRow] = []
    seen: set[tuple[int, str, str]] = set(defaults.row_keys)
    secondary = 0
    skipped = 0

    for entry in result.entries:
        if entry.status != STATUS_LINE:
            continue
        if entry.resolution.needs_review and not include_review and not entry.overridden:
            skipped += 1
            continue
        for assignment in entry.resolution.assignments:
            channel = catalog.get(assignment.channel_number)
            if channel is None or not channel.accepts_content:
                continue
            row = DefaultRow(
                channel_number=assignment.channel_number,
                channel_name=channel.name,
                title=entry.title,
                release_year=entry.year,
            )
            key = row.key()
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
            if not assignment.primary:
                secondary += 1

    rows.sort(key=lambda r: (r.channel_number, r.title.casefold()))
    return rows, secondary, skipped


def _write(path: Path, rows: list[DefaultRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(row.as_csv_row() for row in rows)
    tmp.replace(path)


def assert_additive(original: list[DefaultRow], merged: list[DefaultRow]) -> None:
    """Spec S7: verify on write that the original row set is a subset of the output."""
    original_keys = {row.key() for row in original}
    merged_keys = {row.key() for row in merged}
    missing = original_keys - merged_keys
    if missing:
        sample = sorted(missing)[:5]
        raise IntegrityError(
            f"{len(missing)} original rows would be lost. Refusing to write. First: {sample}"
        )


def export(
    result: ScanResult,
    catalog: ChannelCatalog,
    defaults: DefaultAssignments,
    additions_path: str | Path,
    merged_path: str | Path,
    include_review: bool = False,
) -> ExportReport:
    """Write the additions-only file and the merged full file (spec S7)."""
    additions, secondary, skipped = build_addition_rows(
        result, catalog, defaults, include_review=include_review
    )
    merged = list(defaults.rows) + additions
    assert_additive(defaults.rows, merged)

    additions_p = Path(additions_path)
    merged_p = Path(merged_path)
    _write(additions_p, additions)
    _write(merged_p, merged)

    return ExportReport(
        additions_path=str(additions_p),
        merged_path=str(merged_p),
        additions=len(additions),
        merged_rows=len(merged),
        original_rows=len(defaults.rows),
        secondary_rows=secondary,
        skipped_review=skipped,
    )
