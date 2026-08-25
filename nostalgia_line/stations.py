"""Custom stations - user-created channels that borrow another lineup.

NostalgiaTV lets a user add their own channels. Nostalgia Line lets them say what
such a station *is*, in the same vocabulary the resolution cascade already speaks:

    "Channel 200 'Retro Gaming' should use the lineup for G4."
    "Channel 201 'Saturday Mornings' should mirror Boomer-Rang."

Two kinds of source, freely combined:

* ``source_networks`` - real TMDB network names (``G4``, ``TechTV``, ``Toonami``).
  Anything TMDB says aired on that network routes here. This is how a station
  claims a network that has no stock NostalgiaTV analogue.
* ``source_channels`` - existing NostalgiaTV channel numbers. The station inherits
  whatever would route to that channel.

And two modes:

* ``claim``  - the station takes the title *instead of* the original channel.
* ``mirror`` - the station takes it *as well*, as a secondary assignment.

``claim`` is the default: a user who bothers to point a station at G4 wants G4
content on it, not on whatever generic channel it would otherwise land on.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .channels import Channel, ChannelCatalog

CLAIM = "claim"
MIRROR = "mirror"
MODES = (CLAIM, MIRROR)

# Custom stations live above the stock 1001-1113 band by default so they cannot
# collide with a future NostalgiaTV channel.
CUSTOM_BAND_START = 1200

_SLUG = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    return _SLUG.sub("_", name.strip().lower()).strip("_") or "station"


@dataclass
class CustomStation:
    """A user-defined channel and the lineup it borrows."""

    number: int
    name: str
    source_networks: list[str] = field(default_factory=list)
    source_channels: list[int] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    mode: str = CLAIM
    enabled: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"station {self.name!r}: mode must be one of {MODES}, got {self.mode!r}")
        if self.number <= 0:
            raise ValueError(f"station {self.name!r}: number must be positive")
        if not self.name.strip():
            raise ValueError("station name may not be blank")
        self.source_networks = [n.strip() for n in self.source_networks if n and n.strip()]
        self.keywords = [k.strip().lower() for k in self.keywords if k and k.strip()]
        self.source_channels = [int(c) for c in self.source_channels]

    @property
    def app_key(self) -> str:
        return f"custom_{_slugify(self.name)}"

    def as_channel(self) -> Channel:
        return Channel(
            number=self.number,
            app_key=self.app_key,
            name=self.name,
            category="custom",
            accepts_content=True,
        )

    def matches_network(self, network: str | None) -> bool:
        if not network:
            return False
        folded = network.casefold()
        return any(n.casefold() == folded for n in self.source_networks)

    def matches_channel(self, number: int) -> bool:
        return number in self.source_channels

    def matches_keyword(self, keywords: set[str]) -> bool:
        if not self.keywords:
            return False
        return any(k in keywords for k in self.keywords)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "CustomStation":
        return cls(
            number=int(raw["number"]),
            name=str(raw["name"]),
            source_networks=list(raw.get("source_networks") or []),
            source_channels=[int(c) for c in (raw.get("source_channels") or [])],
            keywords=list(raw.get("keywords") or []),
            mode=str(raw.get("mode") or CLAIM),
            enabled=bool(raw.get("enabled", True)),
            note=str(raw.get("note") or ""),
        )


class StationBook:
    """The user's custom stations, persisted as JSON."""

    def __init__(self, stations: list[CustomStation] | None = None):
        self._stations: dict[int, CustomStation] = {}
        for station in stations or []:
            self._stations[station.number] = station

    # -- persistence -----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "StationBook":
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
        entries = raw.get("stations", raw) if isinstance(raw, dict) else raw
        return cls([CustomStation.from_dict(e) for e in entries])

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stations": [s.to_dict() for s in self]}
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.replace(p)

    # -- collection ------------------------------------------------------

    def __iter__(self):
        return iter(sorted(self._stations.values(), key=lambda s: s.number))

    def __len__(self) -> int:
        return len(self._stations)

    def get(self, number: int) -> CustomStation | None:
        return self._stations.get(number)

    def enabled(self) -> list[CustomStation]:
        return [s for s in self if s.enabled]

    def next_number(self) -> int:
        used = set(self._stations)
        number = CUSTOM_BAND_START
        while number in used:
            number += 1
        return number

    def add(self, station: CustomStation) -> CustomStation:
        if station.number in self._stations:
            raise ValueError(f"channel {station.number} is already a custom station")
        self._stations[station.number] = station
        return station

    def upsert(self, station: CustomStation) -> CustomStation:
        self._stations[station.number] = station
        return station

    def remove(self, number: int) -> bool:
        return self._stations.pop(number, None) is not None

    def register_with(self, catalog: ChannelCatalog) -> None:
        """Make every enabled station routable like a stock channel."""
        for station in self.enabled():
            catalog.add(station.as_channel())

    # -- routing lookups -------------------------------------------------

    def claiming_network(self, network: str | None) -> list[CustomStation]:
        return [s for s in self.enabled() if s.matches_network(network)]

    def claiming_channel(self, number: int) -> list[CustomStation]:
        return [s for s in self.enabled() if s.matches_channel(number)]

    def networks_claimed(self) -> set[str]:
        out: set[str] = set()
        for station in self.enabled():
            out.update(n.casefold() for n in station.source_networks)
        return out

    def validate_against(self, catalog: ChannelCatalog) -> list[str]:
        """Human-readable problems: collisions with stock channels, unknown sources."""
        problems: list[str] = []
        for station in self:
            stock = catalog.get(station.number)
            if stock is not None and stock.category != "custom":
                problems.append(
                    f"station {station.number} '{station.name}' collides with stock channel "
                    f"'{stock.name}'"
                )
            for source in station.source_channels:
                if catalog.get(source) is None:
                    problems.append(
                        f"station '{station.name}' borrows unknown channel {source}"
                    )
            if not (station.source_networks or station.source_channels or station.keywords):
                problems.append(
                    f"station '{station.name}' has no sources, so nothing will route to it"
                )
        return problems
