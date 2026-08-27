"""Scan orchestration: Plex -> TMDB -> cascade -> diff (spec S13)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .cascade import (
    HIGH,
    STATUS_APP,
    STATUS_LINE,
    STATUS_UNASSIGNED,
    Assignment,
    Cascade,
    Resolution,
)
from .channels import ChannelCatalog, DefaultAssignments, strip_year
from .config import Config
from .media import MOVIE, SHOW, MediaItem
from .sources import build_source, source_libraries
from .stations import StationBook
from .tmdb import TMDBCache, TMDBClient

ProgressFn = Callable[[str, int, int], None]


@dataclass
class LibraryEntry:
    """One row of the media-library view (spec S9)."""

    uid: str
    title: str
    year: int | None
    type: str
    section: str
    episode_count: int
    tmdb_id: int | None
    resolution: Resolution
    overview: str = ""
    poster_path: str = ""
    network: str | None = None
    genres: list[str] = field(default_factory=list)
    origin_country: list[str] = field(default_factory=list)
    season_count: int = 0
    overridden: bool = False
    # How this entry compares with the previous scan: "new", "changed",
    # "unchanged" - or "" when there was no previous scan to compare against.
    delta: str = ""

    @property
    def status(self) -> str:
        return self.resolution.status

    @property
    def mapping_source(self) -> str:
        """Who decided this placement.

        lineup  - already in the channels.csv you imported
        auto    - the cascade placed it
        manual  - you placed it by hand
        none    - nothing placed it
        """
        if self.overridden:
            return "manual"
        if self.resolution.status == STATUS_APP:
            return "lineup"
        if self.resolution.status == STATUS_LINE:
            return "auto"
        return "none"

    @property
    def channels(self) -> list[int]:
        if self.resolution.status == STATUS_APP:
            return list(self.resolution.existing_channels)
        return [a.channel_number for a in self.resolution.assignments]

    _STATE_FIELDS = (
        "uid", "title", "year", "type", "section", "episode_count", "season_count",
        "tmdb_id", "overview", "poster_path", "network", "genres", "origin_country",
        "overridden", "delta",
    )

    def to_state(self) -> dict:
        """Serialise for the on-disk scan cache. Deliberately separate from
        to_dict(), which is the API shape and carries derived fields."""
        state = {name: getattr(self, name) for name in self._STATE_FIELDS}
        state["resolution"] = self.resolution.to_dict()
        return state

    @classmethod
    def from_state(cls, raw: dict) -> "LibraryEntry":
        kwargs = {name: raw.get(name) for name in cls._STATE_FIELDS}
        kwargs["genres"] = list(kwargs.get("genres") or [])
        kwargs["origin_country"] = list(kwargs.get("origin_country") or [])
        kwargs["episode_count"] = int(kwargs.get("episode_count") or 0)
        kwargs["season_count"] = int(kwargs.get("season_count") or 0)
        kwargs["overridden"] = bool(kwargs.get("overridden"))
        kwargs["overview"] = kwargs.get("overview") or ""
        kwargs["poster_path"] = kwargs.get("poster_path") or ""
        kwargs["delta"] = kwargs.get("delta") or ""
        return cls(resolution=Resolution.from_dict(raw["resolution"]), **kwargs)

    def to_dict(self, catalog: ChannelCatalog) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "year": self.year,
            "type": self.type,
            "section": self.section,
            "episode_count": self.episode_count,
            "tmdb_id": self.tmdb_id,
            "network": self.network,
            "genres": self.genres,
            "origin_country": self.origin_country,
            "season_count": self.season_count,
            "overview": self.overview,
            "poster_path": self.poster_path,
            "overridden": self.overridden,
            "delta": self.delta,
            "status": self.status,
            "mapping_source": self.mapping_source,
            "channels": [
                {"number": n, "name": catalog.name_of(n)} for n in self.channels
            ],
            **self.resolution.to_dict(),
        }


@dataclass
class ScanResult:
    entries: list[LibraryEntry] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    errors: list[str] = field(default_factory=list)
    # TMDB network name -> logo path, harvested during the scan.
    network_logos: dict[str, str] = field(default_factory=dict)
    network_ids: dict[str, int] = field(default_factory=dict)
    # Delta against the scan before this one (see apply_delta). Zero means
    # there was no previous scan, so per-entry delta carries no information.
    previous_scan_at: float = 0.0
    # Titles the previous scan had and this one does not: {uid, title, year}.
    departed: list[dict] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def stats(self) -> dict:
        by_status = {STATUS_APP: 0, STATUS_LINE: 0, STATUS_UNASSIGNED: 0}
        by_rule: dict[str, int] = {}
        by_confidence: dict[str, int] = {}
        review = 0
        new = changed = 0
        for entry in self.entries:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            if entry.resolution.needs_review:
                review += 1
            if entry.delta == "new":
                new += 1
            elif entry.delta == "changed":
                changed += 1
            for assignment in entry.resolution.assignments:
                by_rule[assignment.rule] = by_rule.get(assignment.rule, 0) + 1
                by_confidence[assignment.confidence] = by_confidence.get(assignment.confidence, 0) + 1
        total = len(self.entries)
        placed = by_status[STATUS_APP] + by_status[STATUS_LINE]
        return {
            "total": total,
            "already_assigned": by_status[STATUS_APP],
            "assigned_by_line": by_status[STATUS_LINE],
            "unassigned": by_status[STATUS_UNASSIGNED],
            "needs_review": review,
            "coverage_pct": round(100.0 * placed / total, 1) if total else 0.0,
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "by_confidence": by_confidence,
            "duration_sec": round(self.duration, 1),
            "sections": self.sections,
            "errors": self.errors,
            "since_last_scan": new + changed,
            "delta": {
                "tracked": self.previous_scan_at > 0,
                "new": new,
                "changed": changed,
                "departed": len(self.departed),
                "since": self.previous_scan_at or None,
            },
        }

    def channel_rollup(self, catalog: ChannelCatalog, defaults: DefaultAssignments) -> list[dict]:
        """Channel-by-channel counts, flagging thin and empty channels (spec S9)."""
        existing_counts: dict[int, int] = {}
        for row in defaults.rows:
            existing_counts[row.channel_number] = existing_counts.get(row.channel_number, 0) + 1
        added: dict[int, int] = {}
        for entry in self.entries:
            if entry.status != STATUS_LINE:
                continue
            for number in entry.channels:
                added[number] = added.get(number, 0) + 1
        rows = []
        for channel in catalog:
            existing = existing_counts.get(channel.number, 0)
            new = added.get(channel.number, 0)
            total = existing + new
            rows.append(
                {
                    "number": channel.number,
                    "name": channel.name,
                    "category": channel.category,
                    "accepts_content": channel.accepts_content,
                    "existing": existing,
                    "added": new,
                    "total": total,
                    "empty": channel.accepts_content and total == 0,
                    "thin": channel.accepts_content and 0 < total <= 3,
                }
            )
        return rows

    # Bumped whenever the on-disk shape changes, so an old cache is discarded
    # rather than half-loaded. v2: per-entry delta + genre suggestions - a v1
    # scan also predates the genre rule change, so showing it would present
    # placements the current rules would not make.
    STATE_VERSION = 2

    def save(self, path) -> None:
        """Persist the scan so a container restart does not force a re-scan.

        Written gzipped: an 800-title library is a few MB of JSON, most of it
        overview text.
        """
        import gzip
        import json
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.STATE_VERSION,
            "sections": self.sections,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "errors": self.errors,
            "network_logos": self.network_logos,
            "network_ids": self.network_ids,
            "previous_scan_at": self.previous_scan_at,
            "departed": self.departed,
            "entries": [e.to_state() for e in self.entries],
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(target)

    @classmethod
    def load(cls, path) -> "ScanResult | None":
        """Read a persisted scan back, or None if there is nothing usable."""
        import gzip
        import json
        from pathlib import Path

        source = Path(path)
        if not source.exists():
            return None
        try:
            with gzip.open(source, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError, EOFError):
            return None
        if payload.get("version") != cls.STATE_VERSION:
            return None
        try:
            entries = [LibraryEntry.from_state(e) for e in payload.get("entries", [])]
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            entries=entries,
            sections=list(payload.get("sections") or []),
            started_at=float(payload.get("started_at") or 0.0),
            finished_at=float(payload.get("finished_at") or 0.0),
            errors=list(payload.get("errors") or []),
            network_logos=dict(payload.get("network_logos") or {}),
            network_ids={k: int(v) for k, v in (payload.get("network_ids") or {}).items()},
            previous_scan_at=float(payload.get("previous_scan_at") or 0.0),
            departed=list(payload.get("departed") or []),
        )

    def review_queue(self) -> list[LibraryEntry]:
        return [e for e in self.entries if e.resolution.needs_review and not e.overridden]

    def channel_titles(self, number: int, catalog: ChannelCatalog) -> list[dict]:
        """Every library title currently placed on a channel."""
        rows = []
        for entry in self.entries:
            if number in entry.channels:
                primary = entry.resolution.primary
                rows.append(
                    {
                        "uid": entry.uid,
                        "title": entry.title,
                        "year": entry.year,
                        "episode_count": entry.episode_count,
                        "season_count": entry.season_count,
                        "network": entry.network,
                        "poster_path": entry.poster_path,
                        "tmdb_id": entry.tmdb_id,
                        "mapping_source": entry.mapping_source,
                        "needs_review": entry.resolution.needs_review,
                        "confidence": entry.resolution.confidence,
                        # A lineup title never ran through the cascade, so it has
                        # no rule to cite. Saying where it came from beats a blank.
                        "reason": (
                            primary.reason
                            if primary
                            else "already on this channel in your channels.csv"
                        ),
                        "other_channels": [
                            {"number": n, "name": catalog.name_of(n)}
                            for n in entry.channels
                            if n != number
                        ],
                    }
                )
        rows.sort(key=lambda r: r["title"].casefold())
        return rows

    def network_rollup(
        self, network_map, orphan_map, catalog: ChannelCatalog, stations=None
    ) -> list[dict]:
        """Every TMDB network in the library, with where it currently routes.

        This is the leverage point. One unmapped network can strand forty titles;
        deciding it once is worth forty individual decisions, which is exactly the
        drudgery the spec calls untenable.

        A network claimed by a custom station reports status "station": in the
        cascade that claim beats the network map, so showing the map's answer
        would describe a route nothing takes.
        """
        buckets: dict[str, list[LibraryEntry]] = {}
        for entry in self.entries:
            if entry.network:
                buckets.setdefault(entry.network, []).append(entry)

        rows = []
        for network, group in buckets.items():
            mapped = network_map.get(network, _countries(group))
            orphan = orphan_map.get(network.casefold())
            claims = stations.claiming_network(network) if stations else []
            if claims:
                status = "station"
            elif network_map.is_overridden(network):
                status = "custom"
            elif mapped:
                status = "mapped"
            elif orphan:
                status = "orphan"
            else:
                status = "unmapped"

            if claims:
                target = (claims[0].number, claims[0].name)
            else:
                target = mapped or (orphan[:2] if orphan else None)
            landing: dict[int, int] = {}
            for entry in group:
                for number in entry.channels:
                    landing[number] = landing.get(number, 0) + 1

            rows.append(
                {
                    "network": network,
                    "titles": len(group),
                    "episodes": sum(e.episode_count for e in group),
                    "status": status,
                    "channel_number": target[0] if target else None,
                    # A claiming station names itself: it may not be registered
                    # with the catalog, and the catalog would say "Unknown".
                    "channel_name": (
                        target[1] if claims else catalog.name_of(target[0])
                    ) if target else None,
                    "needs_review": sum(1 for e in group if e.resolution.needs_review),
                    "unassigned": sum(1 for e in group if e.status == STATUS_UNASSIGNED),
                    "already_assigned": sum(1 for e in group if e.status == STATUS_APP),
                    "landing": [
                        {"number": n, "name": catalog.name_of(n), "titles": c}
                        for n, c in sorted(landing.items(), key=lambda kv: -kv[1])
                    ],
                    "has_logo": network in self.network_logos,
                    "samples": [e.title for e in group[:6]],
                }
            )

        # Worst first: unmapped networks with the most titles are the best use of
        # the user's attention. A station claim is as settled as a stock mapping.
        rank = {"unmapped": 0, "orphan": 1, "custom": 2, "station": 3, "mapped": 3}
        rows.sort(key=lambda r: (rank[r["status"]], -r["titles"]))
        return rows

    def diagnostics(self) -> dict:
        """Silent failure modes worth surfacing before the user blames the routing."""
        no_tmdb = [e for e in self.entries if not e.tmdb_id]
        no_network = [
            e for e in self.entries if e.tmdb_id and not e.network and e.type == SHOW
        ]
        return {
            "no_tmdb_id": len(no_tmdb),
            "no_tmdb_samples": [e.title for e in no_tmdb[:8]],
            "no_network": len(no_network),
            "no_network_samples": [e.title for e in no_network[:8]],
        }


def _countries(entries: list["LibraryEntry"]) -> list[str]:
    """Union of origin countries across a network's titles, for map disambiguation."""
    out: set[str] = set()
    for entry in entries:
        out.update(entry.origin_country)
    return sorted(out)


def _episode_count(item: MediaItem, tmdb_record) -> int:
    if item.episode_count:
        return item.episode_count
    return getattr(tmdb_record, "episode_count", 0) or 0


async def run_scan(
    cfg: Config,
    catalog: ChannelCatalog,
    defaults: DefaultAssignments,
    stations: StationBook,
    overrides: dict[str, list[int]] | None = None,
    network_overrides: dict[str, int] | None = None,
    include_movies: bool = False,
    progress: ProgressFn | None = None,
) -> ScanResult:
    """Full pipeline. Shows only unless ``include_movies`` is set (1.0 scope)."""
    overrides = overrides or {}
    result = ScanResult(started_at=time.time())

    def report(phase: str, done: int, total: int) -> None:
        if progress:
            progress(phase, done, total)

    types = (SHOW, MOVIE) if include_movies else (SHOW,)

    source = build_source(cfg)
    report(source.name, 0, 0)
    items, sections = await source.fetch_library(source_libraries(cfg) or None, types=types)
    result.sections = [s.title for s in sections]
    report(source.name, len(items), len(items))

    cache = TMDBCache(cfg.path(cfg.data.cache_dir))
    tmdb = TMDBClient(cfg.tmdb.api_key, cache, rate_limit=cfg.tmdb.rate_limit)

    show_ids = [i.tmdb_id for i in items if i.is_show and i.tmdb_id]
    movie_ids = [i.tmdb_id for i in items if not i.is_show and i.tmdb_id]

    series_map = await tmdb.series(show_ids, progress=lambda d, t: report("tmdb_shows", d, t))
    movie_map = {}
    if include_movies and movie_ids:
        movie_map = await tmdb.movies(movie_ids, progress=lambda d, t: report("tmdb_movies", d, t))

    cascade = Cascade.from_config(
        cfg, catalog, defaults, stations, network_overrides=network_overrides
    )

    report("resolve", 0, len(items))
    for index, item in enumerate(items, start=1):
        base_title, suffix_year = strip_year(item.title)
        year = item.year or suffix_year
        if item.is_show:
            record = series_map.get(item.tmdb_id) if item.tmdb_id else None
            resolution = cascade.resolve_series(base_title, year, record)
        else:
            record = movie_map.get(item.tmdb_id) if item.tmdb_id else None
            resolution = cascade.resolve_movie(base_title, year, record)

        if record is not None:
            result.network_logos.update(getattr(record, "network_logos", None) or {})
            result.network_ids.update(getattr(record, "network_ids", None) or {})

        entry = LibraryEntry(
            uid=item.uid,
            title=base_title,
            year=year,
            type=item.type,
            section=item.section,
            episode_count=_episode_count(item, record),
            tmdb_id=item.tmdb_id,
            resolution=resolution,
            overview=(getattr(record, "overview", "") or item.summary)[:600],
            poster_path=getattr(record, "poster_path", "") or "",
            network=resolution.network,
            genres=list(getattr(record, "genres", None) or item.genres),
            origin_country=list(getattr(record, "origin_country", None) or []),
            season_count=item.season_count,
        )
        apply_override(entry, overrides.get(entry.uid), catalog)
        result.entries.append(entry)
        if index % 100 == 0:
            report("resolve", index, len(items))

    report("resolve", len(items), len(items))
    result.finished_at = time.time()
    return result


def apply_delta(current: ScanResult, previous: ScanResult | None) -> None:
    """Mark every entry new/changed/unchanged against the previous scan.

    Keyed on ``LibraryEntry.uid`` and nothing else: uid is stable across scans
    and across a Plex-to-Jellyfin move by design - that is why the no-guid
    fallback is ``local:`` rather than ``plex:``. "Changed" means the
    resolution moved - a different status or channel set - which is what
    matters after a station remap.

    With no previous scan there is nothing to compare against, so delta stays
    "" everywhere rather than declaring an entire first import "new".
    """
    if previous is None or not previous.entries:
        return
    before = {e.uid: e for e in previous.entries}
    for entry in current.entries:
        old = before.get(entry.uid)
        if old is None:
            entry.delta = "new"
        elif (old.status, sorted(old.channels)) != (entry.status, sorted(entry.channels)):
            entry.delta = "changed"
        else:
            entry.delta = "unchanged"
    still_here = {e.uid for e in current.entries}
    current.departed = [
        {"uid": e.uid, "title": e.title, "year": e.year}
        for e in previous.entries
        if e.uid not in still_here
    ]
    current.previous_scan_at = previous.finished_at


def apply_override(entry: LibraryEntry, channels: list[int] | None, catalog: ChannelCatalog) -> None:
    """A human decision replaces whatever the cascade produced (spec S10.3)."""
    if channels is None:
        return
    entry.overridden = True
    entry.resolution.needs_review = False
    entry.resolution.review_reason = ""
    entry.resolution.suggestion = None
    if not channels:
        entry.resolution.status = STATUS_UNASSIGNED
        entry.resolution.assignments = []
        return
    entry.resolution.status = STATUS_LINE
    entry.resolution.assignments = [
        Assignment(
            channel_number=number,
            channel_name=catalog.name_of(number),
            rule="manual_override",
            confidence=HIGH,
            reason="assigned by hand",
            primary=(position == 0),
        )
        for position, number in enumerate(channels)
    ]
