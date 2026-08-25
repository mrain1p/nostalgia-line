"""Channel logo import.

Artwork arrives named however its source named it — ``logo_seaw.png``,
``1021.png``, ``SeaW.PNG`` — so the importer matches filenames to channels rather
than demanding a convention. Files land in ``/config/logos`` named by channel
number, which is what the serving endpoint looks for first.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .channels import ChannelCatalog

IMAGE_SUFFIXES = {".png", ".webp", ".svg", ".jpg", ".jpeg", ".gif"}
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 400

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """Fold a filename or channel name to a comparable slug."""
    stem = Path(name).stem.lower()
    stem = re.sub(r"^(logo|channel|ch)[-_]+", "", stem)
    return _NON_ALNUM.sub("", stem)


@dataclass
class ImportReport:
    imported: list[dict] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "imported": self.imported,
            "unmatched": self.unmatched,
            "skipped": self.skipped,
            "imported_count": len(self.imported),
            "unmatched_count": len(self.unmatched),
            "skipped_count": len(self.skipped),
        }


class LogoImporter:
    def __init__(self, catalog: ChannelCatalog, directory: str | Path, network_map=None):
        self.catalog = catalog
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.network_map = network_map
        self._index = self._build_index()

    def _build_index(self) -> dict[str, int]:
        """Every name a channel might plausibly be filed under.

        Parody names first, then the real-world networks they stand in for:
        artwork is very often filed under the original network - NostalgiaTV's
        own set ships `logo_tnt.png` for T.N.Tea and `logo_metv.png` for
        Watch-On-Repeat - so matching on the parody name alone misses them.
        """
        index: dict[str, int] = {}
        for channel in self.catalog:
            for key in (
                str(channel.number),
                normalize(channel.name),
                normalize(channel.app_key),
                normalize(channel.app_key.replace("app_", "")),
            ):
                if key:
                    index.setdefault(key, channel.number)

        if self.network_map is not None:
            for network in self.network_map.names():
                mapped = self.network_map.get(network)
                if not mapped:
                    continue
                key = normalize(network)
                if key and self.catalog.get(mapped[0]) is not None:
                    index.setdefault(key, mapped[0])
        return index

    def match(self, filename: str) -> int | None:
        """Which channel does this file belong to, if any?"""
        stem = Path(filename).stem.strip()
        if stem.isdigit() and int(stem) in {c.number for c in self.catalog}:
            return int(stem)
        return self._index.get(normalize(filename))

    def ingest(self, filename: str, data: bytes, report: ImportReport) -> None:
        suffix = Path(filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            report.skipped.append({"file": filename, "why": "not an image"})
            return
        if len(data) > MAX_FILE_BYTES:
            report.skipped.append({"file": filename, "why": "larger than 4 MB"})
            return
        if not data:
            report.skipped.append({"file": filename, "why": "empty"})
            return

        number = self.match(filename)
        if number is None:
            report.unmatched.append(filename)
            return

        # Store under the channel number so serving needs no lookup table, and
        # drop any earlier artwork for the same channel in another format.
        for existing in self.dir.glob(f"{number}.*"):
            existing.unlink(missing_ok=True)
        target = self.dir / f"{number}{suffix}"
        target.write_bytes(data)
        report.imported.append(
            {
                "file": filename,
                "channel": number,
                "channel_name": self.catalog.name_of(number),
                "stored_as": target.name,
            }
        )

    def import_files(self, files: list[tuple[str, bytes]]) -> ImportReport:
        report = ImportReport()
        for filename, data in files[:MAX_FILES]:
            self.ingest(filename, data, report)
        return report

    def import_zip(self, blob: bytes) -> ImportReport:
        report = ImportReport()
        try:
            archive = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            report.skipped.append({"file": "archive", "why": "not a readable zip"})
            return report
        with archive:
            for info in archive.infolist()[:MAX_FILES]:
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name or name.startswith("."):
                    continue
                if info.file_size > MAX_FILE_BYTES:
                    report.skipped.append({"file": name, "why": "larger than 4 MB"})
                    continue
                self.ingest(name, archive.read(info), report)
        return report

    def installed(self) -> dict[int, str]:
        """Channel number -> stored filename, for what is already on disk."""
        out: dict[int, str] = {}
        for path in sorted(self.dir.iterdir() if self.dir.exists() else []):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                number = self.match(path.name)
                if number is not None:
                    out[number] = path.name
        return out

    def clear(self) -> int:
        removed = 0
        for path in self.dir.glob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
