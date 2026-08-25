"""Channel logo import.

Artwork arrives named however its source named it — ``logo_seaw.png``,
``1021.png``, ``SeaW.PNG`` — so the importer matches filenames to channels rather
than demanding a convention. Files land in ``/config/logos`` named by channel
number, which is what the serving endpoint looks for first.
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .channels import ChannelCatalog

IMAGE_SUFFIXES = {".png", ".webp", ".svg", ".jpg", ".jpeg", ".gif"}
CONTENT_TYPE_SUFFIX = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 400

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# #EXTINF:-1 tvg-chno="101" tvg-name="Dizzy Channel" tvg-logo="https://…/app_dizzy_channel/logo"
_EXTINF_ATTR = re.compile(r'([a-zA-Z0-9-]+)="([^"]*)"')
# NostalgiaTV's logo URLs carry the channel's app key: …/api/channels/<key>/logo
_APP_KEY_IN_URL = re.compile(r"/channels/(app_[a-z0-9_]+|custom_[a-z0-9_]+)/logo")


@dataclass
class M3UChannel:
    """One #EXTINF line, reduced to what artwork import needs."""

    name: str = ""
    number: int | None = None
    logo_url: str = ""
    app_key: str = ""


def parse_m3u(text: str) -> list[M3UChannel]:
    """Pull channel names, numbers and logo URLs out of an M3U playlist.

    M3U is a stable interop format - unlike a private HTTP API, it is meant to be
    read by other software - which makes it a safe thing to depend on.
    """
    out: list[M3UChannel] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#EXTINF"):
            continue
        attrs = dict(_EXTINF_ATTR.findall(line))
        logo = attrs.get("tvg-logo", "").strip()
        name = (attrs.get("tvg-name") or "").strip()
        if not name and "," in line:
            name = line.rsplit(",", 1)[1].strip()
        raw_number = (attrs.get("tvg-chno") or attrs.get("tvg-id") or "").strip()
        key_match = _APP_KEY_IN_URL.search(logo)
        out.append(
            M3UChannel(
                name=name,
                number=int(raw_number) if raw_number.isdigit() else None,
                logo_url=logo,
                app_key=key_match.group(1) if key_match else "",
            )
        )
    return out


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

    def match_channel(self, entry: "M3UChannel") -> int | None:
        """Resolve an M3U entry to a channel.

        The app key is definitive when present - NostalgiaTV puts it in the logo
        URL - so it is tried first, ahead of the display name.
        """
        if entry.app_key:
            channel = self.catalog.by_app_key(entry.app_key)
            if channel is not None:
                return channel.number
        if entry.name:
            by_name = self._index.get(normalize(entry.name))
            if by_name is not None:
                return by_name
        return None

    def store(self, number: int, data: bytes, suffix: str) -> str:
        """Write artwork for a channel, replacing any earlier format."""
        suffix = suffix if suffix in IMAGE_SUFFIXES else ".png"
        for existing in self.dir.glob(f"{number}.*"):
            existing.unlink(missing_ok=True)
        target = self.dir / f"{number}{suffix}"
        target.write_bytes(data)
        return target.name

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

    async def import_from_m3u(self, url: str, concurrency: int = 8) -> ImportReport:
        """Import artwork straight from an M3U playlist.

        NostalgiaTV publishes one for IPTV clients, with a `tvg-logo` per channel
        and the channel's app key in the URL. That makes it an exact, public,
        no-credentials source of the real artwork - and M3U is an interop format
        rather than a private API, so depending on it is safe.
        """
        report = ImportReport()
        if not url.lower().startswith(("http://", "https://")):
            report.skipped.append({"file": url, "why": "not an http(s) URL"})
            return report

        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                report.skipped.append({"file": url, "why": f"could not fetch: {exc}"})
                return report

            entries = parse_m3u(response.text)
            wanted: list[tuple[M3UChannel, int]] = []
            for entry in entries:
                number = self.match_channel(entry)
                if number is None:
                    report.unmatched.append(entry.name or entry.logo_url)
                elif entry.logo_url:
                    wanted.append((entry, number))

            semaphore = asyncio.Semaphore(concurrency)
            seen: set[int] = set()

            async def grab(entry: M3UChannel, number: int) -> None:
                if number in seen:
                    return
                seen.add(number)
                async with semaphore:
                    try:
                        art = await client.get(entry.logo_url)
                    except httpx.HTTPError as exc:
                        report.skipped.append({"file": entry.name, "why": str(exc)[:80]})
                        return
                if art.status_code != 200 or not art.content:
                    report.skipped.append({"file": entry.name, "why": f"HTTP {art.status_code}"})
                    return
                if len(art.content) > MAX_FILE_BYTES:
                    report.skipped.append({"file": entry.name, "why": "larger than 4 MB"})
                    return
                suffix = CONTENT_TYPE_SUFFIX.get(
                    art.headers.get("content-type", "").split(";")[0].strip(), ".png"
                )
                stored = self.store(number, art.content, suffix)
                report.imported.append(
                    {
                        "file": entry.name,
                        "channel": number,
                        "channel_name": self.catalog.name_of(number),
                        "stored_as": stored,
                    }
                )

            await asyncio.gather(*(grab(e, n) for e, n in wanted[:MAX_FILES]))
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
