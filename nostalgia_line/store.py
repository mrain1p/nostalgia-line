"""Persistent state: manual overrides and review decisions (spec S10.3)."""
from __future__ import annotations

import json
from pathlib import Path


class Store:
    """A small JSON document that survives restarts.

    Keyed by ``LibraryEntry.uid`` (``tmdb:show:1234``), so a decision sticks even
    if Plex re-scans and hands out new rating keys.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.overrides: dict[str, list[int]] = {}
        self.dismissed: set[str] = set()
        # TMDB network name (casefolded) -> channel number. Layered over the
        # shipped network_map.csv, so one decision covers every title on it.
        self.networks: dict[str, int] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        self.overrides = {
            str(k): [int(n) for n in v] for k, v in (raw.get("overrides") or {}).items()
        }
        self.dismissed = {str(u) for u in (raw.get("dismissed") or [])}
        self.networks = {
            str(k).casefold(): int(v) for k, v in (raw.get("networks") or {}).items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "overrides": self.overrides,
            "dismissed": sorted(self.dismissed),
            "networks": self.networks,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.replace(self.path)

    def set_override(self, uid: str, channels: list[int]) -> None:
        self.overrides[uid] = sorted(dict.fromkeys(int(c) for c in channels))
        self.save()

    def clear_override(self, uid: str) -> bool:
        removed = self.overrides.pop(uid, None) is not None
        if removed:
            self.save()
        return removed

    def dismiss(self, uid: str) -> None:
        self.dismissed.add(uid)
        self.save()

    def undismiss(self, uid: str) -> None:
        self.dismissed.discard(uid)
        self.save()

    def map_network(self, network: str, channel: int) -> None:
        self.networks[network.casefold()] = int(channel)
        self.save()

    def unmap_network(self, network: str) -> bool:
        removed = self.networks.pop(network.casefold(), None) is not None
        if removed:
            self.save()
        return removed

    def stats(self) -> dict[str, int]:
        return {
            "overrides": len(self.overrides),
            "dismissed": len(self.dismissed),
            "networks": len(self.networks),
        }
