"""Channel catalog, the shipped default assignments, and title matching (spec S4, S6, S7)."""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

# Channels that exist but never receive routed content (spec S4: 1072-1088).
NO_CONTENT_CATEGORIES = frozenset({"music", "utility"})

_YEAR_SUFFIX = re.compile(r"\s*\((\d{4})\)\s*$")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_year(title: str) -> tuple[str, int | None]:
    """Split Plex's disambiguating year suffix: 'Our Planet (2019)' -> ('Our Planet', 2019)."""
    match = _YEAR_SUFFIX.search(title)
    if not match:
        return title.strip(), None
    return title[: match.start()].strip(), int(match.group(1))


def normalize_title(title: str) -> str:
    """Normalize a title for fallback matching (spec S7).

    Strips a trailing (YYYY), strips a leading article, lowercases, and removes
    every non-alphanumeric character. Only ever a fallback - prefer tmdb_id.
    """
    base, _ = strip_year(title)
    base = _LEADING_ARTICLE.sub("", base.strip())
    return _NON_ALNUM.sub("", base.lower())


@dataclass(frozen=True)
class Channel:
    number: int
    app_key: str
    name: str
    category: str
    accepts_content: bool

    @property
    def no_content(self) -> bool:
        return not self.accepts_content


@dataclass(frozen=True)
class DefaultRow:
    """One row of the user's shipped channels.csv. Authoritative and immutable."""

    channel_number: int
    channel_name: str
    title: str
    release_year: int | None

    def as_csv_row(self) -> list[str]:
        return [
            str(self.channel_number),
            self.channel_name,
            self.title,
            "" if self.release_year is None else str(self.release_year),
        ]

    def key(self) -> tuple[int, str, str]:
        return (self.channel_number, self.title, str(self.release_year or ""))


class ChannelCatalog:
    """The 113 NostalgiaTV channels, 1001-1113, plus any user custom stations."""

    def __init__(self, channels: list[Channel]):
        self._by_number = {c.number: c for c in channels}
        self._by_name = {c.name.casefold(): c for c in channels}
        self._by_key = {c.app_key: c for c in channels}

    @classmethod
    def load(cls, path: str | Path) -> "ChannelCatalog":
        channels: list[Channel] = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                channels.append(
                    Channel(
                        number=int(row["channel_number"]),
                        app_key=row["app_key"],
                        name=row["channel_name"],
                        category=row["category"],
                        accepts_content=row["accepts_content"].strip().lower() == "true",
                    )
                )
        if not channels:
            raise ValueError(f"empty channel catalog: {path}")
        return cls(channels)

    def __len__(self) -> int:
        return len(self._by_number)

    def __iter__(self):
        return iter(sorted(self._by_number.values(), key=lambda c: c.number))

    def get(self, number: int) -> Channel | None:
        return self._by_number.get(number)

    def require(self, number: int) -> Channel:
        channel = self._by_number.get(number)
        if channel is None:
            raise KeyError(f"no channel numbered {number}")
        return channel

    def by_name(self, name: str) -> Channel | None:
        return self._by_name.get(name.casefold())

    def by_app_key(self, key: str) -> Channel | None:
        return self._by_key.get(key)

    def name_of(self, number: int) -> str:
        channel = self._by_number.get(number)
        return channel.name if channel else f"Unknown {number}"

    def routable(self) -> list[Channel]:
        return [c for c in self if c.accepts_content]

    def add(self, channel: Channel) -> None:
        """Register a custom station so it can be routed to like any other channel."""
        self._by_number[channel.number] = channel
        self._by_name[channel.name.casefold()] = channel
        self._by_key[channel.app_key] = channel

    def remove(self, number: int) -> None:
        channel = self._by_number.pop(number, None)
        if channel is None:
            return
        self._by_name.pop(channel.name.casefold(), None)
        self._by_key.pop(channel.app_key, None)


class DefaultAssignments:
    """The user's existing channels.csv - read-only, authoritative (spec S7).

    The shipped file carries no tmdb ids, so library items are matched against it
    on normalized title plus year, with a year-less fallback. The 21 known
    same-title collisions (Aladdin the 1992 film vs the 1994 series, etc.) are kept
    as distinct rows and disambiguated by year.
    """

    def __init__(self, rows: list[DefaultRow]):
        self.rows = rows
        self._by_title_year: dict[tuple[str, int | None], list[DefaultRow]] = defaultdict(list)
        self._by_title: dict[str, list[DefaultRow]] = defaultdict(list)
        for row in rows:
            norm = normalize_title(row.title)
            self._by_title_year[(norm, row.release_year)].append(row)
            self._by_title[norm].append(row)
        self._row_keys = {row.key() for row in rows}
        self._sanctioned_pairs = self._derive_sanctioned_pairs()

    @classmethod
    def load(cls, path: str | Path) -> "DefaultAssignments":
        rows: list[DefaultRow] = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            expected = ["Channel Number", "Channel Name", "Title", "Release Year"]
            if [f.strip() for f in (reader.fieldnames or [])] != expected:
                raise ValueError(f"{path}: expected header {expected}, got {reader.fieldnames}")
            for row in reader:
                year_raw = (row.get("Release Year") or "").strip()
                rows.append(
                    DefaultRow(
                        channel_number=int(row["Channel Number"]),
                        channel_name=(row["Channel Name"] or "").strip(),
                        title=(row["Title"] or "").strip(),
                        release_year=int(year_raw) if year_raw.isdigit() else None,
                    )
                )
        return cls(rows)

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def row_keys(self) -> set[tuple[int, str, str]]:
        return set(self._row_keys)

    def lookup(self, title: str, year: int | None) -> list[DefaultRow]:
        """Find existing rows for a library title. Exact title+year first, then title alone."""
        norm = normalize_title(title)
        if year is not None:
            exact = self._by_title_year.get((norm, year))
            if exact:
                return list(exact)
            # Plex and the default file can disagree by a year at the air-date boundary.
            for delta in (-1, 1):
                near = self._by_title_year.get((norm, year + delta))
                if near:
                    return list(near)
        candidates = self._by_title.get(norm, [])
        if year is not None and len({c.release_year for c in candidates}) > 1:
            # Ambiguous same-title collision and no year agreement: do not guess.
            return []
        return list(candidates)

    def channels_for(self, title: str, year: int | None) -> set[int]:
        return {row.channel_number for row in self.lookup(title, year)}

    def titles_on_channel(self, number: int) -> list[DefaultRow]:
        return [row for row in self.rows if row.channel_number == number]

    def _derive_sanctioned_pairs(self) -> set[frozenset[int]]:
        """Channel pairings the shipped file already sanctions (spec S6).

        Derived at runtime from the user's own channels.csv so it adapts if the
        NostalgiaTV defaults change.
        """
        per_title: dict[tuple[str, int | None], set[int]] = defaultdict(set)
        for row in self.rows:
            per_title[(normalize_title(row.title), row.release_year)].add(row.channel_number)
        pairs: set[frozenset[int]] = set()
        for channels in per_title.values():
            if len(channels) > 1:
                pairs.update(frozenset(pair) for pair in combinations(sorted(channels), 2))
        return pairs

    @property
    def sanctioned_pairs(self) -> set[frozenset[int]]:
        return set(self._sanctioned_pairs)

    def is_sanctioned_pair(self, a: int, b: int) -> bool:
        return frozenset((a, b)) in self._sanctioned_pairs

    def multi_channel_stats(self) -> dict[str, int]:
        per_title: dict[tuple[str, int | None], set[int]] = defaultdict(set)
        for row in self.rows:
            per_title[(normalize_title(row.title), row.release_year)].add(row.channel_number)
        multi = [c for c in per_title.values() if len(c) > 1]
        return {
            "titles": len(per_title),
            "multi_channel_titles": len(multi),
            "exactly_two": sum(1 for c in multi if len(c) == 2),
            "three_or_more": sum(1 for c in multi if len(c) >= 3),
            "sanctioned_pairs": len(self._sanctioned_pairs),
        }


class NetworkMap:
    """TMDB network name -> channel, disambiguated by country of origin (spec S12).

    Network names are not unique across countries. TMDB lists a US ``TBS`` and a
    Japanese ``TBS``; mapping on name alone drops anime onto an American cable
    channel. Rows may carry an optional ``origin_country``; a row with a country
    only matches a series from that country, and an unqualified row matches
    anything. The more specific row always wins.
    """

    def __init__(self, rows: list[tuple[str, str, int, str]]):
        # name -> list of (country, number, name)
        self._rows: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
        for network, country, number, channel_name in rows:
            self._rows[network.casefold()].append((country.upper(), number, channel_name))
        # User decisions, layered on top of the shipped file. Always win.
        self._overrides: dict[str, tuple[int, str]] = {}

    def set_override(self, network: str, number: int, channel_name: str) -> None:
        self._overrides[network.casefold()] = (number, channel_name)

    def clear_override(self, network: str) -> bool:
        return self._overrides.pop(network.casefold(), None) is not None

    def is_overridden(self, network: str) -> bool:
        return network.casefold() in self._overrides

    def apply_overrides(self, mapping: dict[str, int], catalog: "ChannelCatalog") -> None:
        """Replace the whole override layer from persisted state."""
        self._overrides.clear()
        for network, number in mapping.items():
            channel = catalog.get(int(number))
            if channel is not None:
                self.set_override(network, channel.number, channel.name)

    def __contains__(self, network: object) -> bool:
        if not isinstance(network, str):
            return False
        folded = network.casefold()
        return folded in self._rows or folded in self._overrides

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def get(
        self, network: str | None, origin_country: list[str] | None = None
    ) -> tuple[int, str] | None:
        if not network:
            return None
        if override := self._overrides.get(network.casefold()):
            return override
        candidates = self._rows.get(network.casefold())
        if not candidates:
            return None
        countries = {c.upper() for c in (origin_country or [])}
        for country, number, name in candidates:
            if country and country in countries:
                return (number, name)
        for country, number, name in candidates:
            if not country:
                return (number, name)
        # A country tag disambiguates between rivals; with only one candidate
        # there is nothing to disambiguate, so use it (TMDB's NHK is Japan's NHK
        # whether or not the series carries an origin_country).
        if len(candidates) == 1:
            _, number, name = candidates[0]
            return (number, name)
        return None

    def names(self) -> list[str]:
        return sorted(set(self._rows) | set(self._overrides))

    def rows(self) -> list[tuple[str, str, int, str]]:
        """Every (network, country, number, name) row, for validation."""
        return [
            (network, country, number, name)
            for network, entries in self._rows.items()
            for country, number, name in entries
        ]


def load_network_map(path: str | Path) -> NetworkMap:
    """Load network_map.csv. The origin_country column is optional."""
    rows: list[tuple[str, str, int, str]] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            network = (row["tmdb_network"] or "").strip()
            if not network:
                continue
            rows.append(
                (
                    network,
                    (row.get("origin_country") or "").strip(),
                    int(row["channel_number"]),
                    (row["channel_name"] or "").strip(),
                )
            )
    return NetworkMap(rows)


def load_orphan_networks(path: str | Path) -> dict[str, tuple[int, str, str]]:
    """Orphan network -> (channel number, channel name, rationale). Spec S5."""
    mapping: dict[str, tuple[int, str, str]] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            network = (row["tmdb_network"] or "").strip()
            if not network:
                continue
            mapping[network.casefold()] = (
                int(row["channel_number"]),
                (row["channel_name"] or "").strip(),
                (row.get("rationale") or "").strip(),
            )
    return mapping
