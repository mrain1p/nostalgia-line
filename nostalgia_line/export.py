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
    # NostalgiaTV's own export is plain UTF-8 with bare LF and no BOM. csv.writer
    # defaults to CRLF, which would make every line differ from the file we were
    # handed - so match it exactly rather than hoping their parser is tolerant.
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(row.as_csv_row() for row in rows)
    tmp.replace(path)


def assert_additive(original: list[DefaultRow], merged: list[DefaultRow]) -> None:
    """Spec S7: verify on write that every original row survives, verbatim.

    Compares the exact field values rather than the dedupe key. The key folds an
    unparseable year to "", so a row whose year said ``Various`` could be
    rewritten as blank and still satisfy a key-based check - which is precisely
    what was happening to 37 rows of the stock file.
    """
    original_rows = {row.exact() for row in original}
    merged_rows = {row.exact() for row in merged}
    missing = original_rows - merged_rows
    if missing:
        sample = sorted(missing)[:5]
        raise IntegrityError(
            f"{len(missing)} original rows would be lost or altered. Refusing to write. "
            f"First: {sample}"
        )


def preflight(
    result: ScanResult,
    catalog: ChannelCatalog,
    defaults: DefaultAssignments,
    include_review: bool = False,
) -> dict:
    """Check a merged file would import cleanly, without writing anything.

    The checks are the ones that actually bit during development: an original
    row silently altered, a year rewritten, or output that does not match the
    conventions of the file NostalgiaTV produced.
    """
    additions, secondary, skipped = build_addition_rows(
        result, catalog, defaults, include_review=include_review
    )
    merged = list(defaults.rows) + additions
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        assert_additive(defaults.rows, merged)
        check("Every original row survives verbatim", True, f"{len(defaults.rows)} rows unchanged")
    except IntegrityError as exc:
        check("Every original row survives verbatim", False, str(exc))

    preserved = [r for r in defaults.rows if r.year_text and not r.year_text.isdigit()]
    check(
        "Non-numeric years kept as written",
        all(r.as_csv_row()[3] == r.year_text for r in preserved),
        f"{len(preserved)} row(s) use a value like 'Various'",
    )

    bad_channel = [r for r in additions if (c := catalog.get(r.channel_number)) is None or not c.accepts_content]
    check(
        "No rows target a channel that holds no content",
        not bad_channel,
        f"{len(bad_channel)} bad row(s)" if bad_channel else "channels 1072-1088 excluded",
    )

    seen: set[tuple] = set()
    dupes = [r for r in merged if r.key() in seen or seen.add(r.key())]
    check("No duplicate rows", not dupes, f"{len(dupes)} duplicate(s)" if dupes else "all rows distinct")

    blank = [r for r in additions if not r.title.strip()]
    check("No blank titles", not blank, f"{len(blank)} blank" if blank else "every row names a title")

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "additions": len(additions),
        "secondary_rows": secondary,
        "skipped_review": skipped,
        "merged_rows": len(merged),
        "original_rows": len(defaults.rows),
    }


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
