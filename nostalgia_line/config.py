"""Configuration loading (spec S11)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

SOURCES = ("plex", "jellyfin")
ROUTING_MODES = ("streaming_first", "hybrid", "themed")
MULTI_CHANNEL_MODES = ("off", "sanctioned_pairs_only", "permissive")
ORPHAN_MODES = ("parent_fallback", "content_type", "flag_only")


@dataclass
class PlexConfig:
    url: str = "http://127.0.0.1:32400"
    token: str = ""
    libraries: list[str] = field(default_factory=list)


@dataclass
class JellyfinConfig:
    url: str = "http://127.0.0.1:8096"
    api_key: str = ""
    user_id: str = ""
    libraries: list[str] = field(default_factory=list)


@dataclass
class NostalgiaTVConfig:
    """Optional pointers at a NostalgiaTV install.

    Only interop formats are used - the M3U playlist - never its private API.
    """

    m3u_url: str = ""
    auto_refresh_logos: bool = True


@dataclass
class TMDBConfig:
    api_key: str = ""
    rate_limit: int = 50


@dataclass
class RoutingConfig:
    # Films are opt-in. They outnumber shows several times over in a typical
    # library, and their routing has no network to lean on - only genre, era and
    # collection - so the results deserve a deliberate choice.
    include_movies: bool = False
    mode: str = "streaming_first"
    multi_channel: str = "sanctioned_pairs_only"
    orphan_network: str = "parent_fallback"

    def validate(self) -> None:
        if self.mode not in ROUTING_MODES:
            raise ValueError(f"routing.mode must be one of {ROUTING_MODES}, got {self.mode!r}")
        if self.multi_channel not in MULTI_CHANNEL_MODES:
            raise ValueError(
                f"routing.multi_channel must be one of {MULTI_CHANNEL_MODES}, got {self.multi_channel!r}"
            )
        if self.orphan_network not in ORPHAN_MODES:
            raise ValueError(
                f"routing.orphan_network must be one of {ORPHAN_MODES}, got {self.orphan_network!r}"
            )


@dataclass
class OutputConfig:
    additions_only: str = "channels_additions.csv"
    merged: str = "channels_merged.csv"


@dataclass
class DataConfig:
    channels_csv: str = "data/channels.csv"
    network_map: str = "data/network_map.csv"
    orphan_networks: str = "data/orphan_networks.csv"
    channel_catalog: str = "data/channel_catalog.csv"
    cache_dir: str = ".cache"
    state_file: str = "state.json"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8777


@dataclass
class Config:
    # Which media server holds the library. NostalgiaTV is never contacted -
    # it only supplies channels.csv, which the user imports and exports.
    source: str = "plex"
    plex: PlexConfig = field(default_factory=PlexConfig)
    jellyfin: JellyfinConfig = field(default_factory=JellyfinConfig)
    nostalgiatv: NostalgiaTVConfig = field(default_factory=NostalgiaTVConfig)
    tmdb: TMDBConfig = field(default_factory=TMDBConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    data: DataConfig = field(default_factory=DataConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    root: Path = field(default_factory=Path.cwd)

    def path(self, value: str) -> Path:
        """Resolve a configured path against the project root."""
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("root", None)
        # never hand secrets to the browser
        d["plex"]["token"] = bool(self.plex.token)
        d["jellyfin"]["api_key"] = bool(self.jellyfin.api_key)
        d["tmdb"]["api_key"] = bool(self.tmdb.api_key)
        return d


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"config section {key!r} must be a mapping")
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config.yaml, falling back to defaults. Env vars win over the file.

    PLEX_URL, PLEX_TOKEN and TMDB_API_KEY are read from the environment so the
    secrets can stay out of the file entirely.
    """
    root = Path(path).parent.resolve() if path else Path.cwd()
    raw: dict[str, Any] = {}
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    cfg = Config(
        source=str(raw.get("source") or "plex").strip().lower(),
        plex=PlexConfig(**_section(raw, "plex")),
        jellyfin=JellyfinConfig(**_section(raw, "jellyfin")),
        nostalgiatv=NostalgiaTVConfig(**_section(raw, "nostalgiatv")),
        tmdb=TMDBConfig(**_section(raw, "tmdb")),
        routing=RoutingConfig(**_section(raw, "routing")),
        output=OutputConfig(**_section(raw, "output")),
        data=DataConfig(**_section(raw, "data")),
        server=ServerConfig(**_section(raw, "server")),
        root=root,
    )

    if env := os.getenv("PLEX_URL"):
        cfg.plex.url = env
    if env := os.getenv("PLEX_TOKEN"):
        cfg.plex.token = env
    if env := os.getenv("TMDB_API_KEY"):
        cfg.tmdb.api_key = env
    if env := os.getenv("JELLYFIN_URL"):
        cfg.jellyfin.url = env
    if env := os.getenv("JELLYFIN_API_KEY"):
        cfg.jellyfin.api_key = env
    if env := os.getenv("NOSTALGIATV_M3U_URL"):
        cfg.nostalgiatv.m3u_url = env
    if env := os.getenv("NOSTALGIA_SOURCE"):
        cfg.source = env.strip().lower()

    if cfg.source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {cfg.source!r}")
    cfg.routing.validate()
    return cfg


def save_config(cfg: Config, path: str | os.PathLike[str]) -> None:
    """Write config back out, preserving secrets already held in memory."""
    payload = {
        "source": cfg.source,
        "plex": asdict(cfg.plex),
        "jellyfin": asdict(cfg.jellyfin),
        "nostalgiatv": asdict(cfg.nostalgiatv),
        "tmdb": asdict(cfg.tmdb),
        "routing": asdict(cfg.routing),
        "output": asdict(cfg.output),
        "data": asdict(cfg.data),
        "server": asdict(cfg.server),
    }
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False)
