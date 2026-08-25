"""FastAPI app. Everything the user needs is in the GUI; the YAML is only a seed."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .auth import (
    COOKIE,
    OPEN_PATHS,
    Sessions,
    hash_password,
    password_from_env,
    verify_password,
)
from .cascade import STATUS_APP, STATUS_LINE, STATUS_UNASSIGNED
from .channels import (
    ChannelCatalog,
    DefaultAssignments,
    load_network_map,
    load_orphan_networks,
    normalize_title,
)
from .config import SOURCES, Config, load_config, save_config
from .export import build_addition_rows, preflight as run_preflight
from .export import export as run_export
from .pipeline import ScanResult, apply_override, run_scan
from .posters import DEFAULT_SIZE, PosterCache
from .logos import LogoImporter
from .media import SourceError
from .sources import build_source, missing_credential_message, source_is_configured
from .stations import CUSTOM_BAND_START, CustomStation, StationBook
from .store import Store
from .workflow import build as build_workflow
from .tmdb import TMDBCache, TMDBClient, TMDBError

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
WEB_DIR = PROJECT_ROOT / "web"


class AppState:
    """Everything the request handlers share. Rebuilt when settings change."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg: Config = load_config(config_path)
        self.catalog = ChannelCatalog.load(self.cfg.path(self.cfg.data.channel_catalog))
        self.defaults = DefaultAssignments.load(self.cfg.path(self.cfg.data.channels_csv))
        self.stations = StationBook.load(self.cfg.path("stations.json"))
        self.stations.register_with(self.catalog)
        self.store = Store(self.cfg.path(self.cfg.data.state_file))
        self.posters = PosterCache(self.cfg.path(self.cfg.data.cache_dir) / "posters")
        self.scan_path = self.cfg.path("scan.json.gz")
        # A restart should not cost a re-scan. The snapshot may be dated, and the
        # UI says so rather than pretending it is live.
        self.result: ScanResult | None = ScanResult.load(self.scan_path)
        self.scan_task: asyncio.Task | None = None
        self.progress: dict[str, Any] = {"phase": "idle", "done": 0, "total": 0}
        self.sessions = Sessions()
        self.last_error: str = ""
        self.last_export: dict | None = None
        # Set whenever a routing input changes. The displayed scan was produced
        # under the old rules, so it no longer reflects what an export would do.
        self.stale: bool = False
        self.stale_reason: str = ""

    def reload_reference_data(self) -> None:
        self.cfg = load_config(self.config_path)
        self.catalog = ChannelCatalog.load(self.cfg.path(self.cfg.data.channel_catalog))
        self.defaults = DefaultAssignments.load(self.cfg.path(self.cfg.data.channels_csv))
        self.stations.register_with(self.catalog)

    def persist_result(self) -> None:
        if self.result is not None:
            try:
                self.result.save(self.scan_path)
            except OSError as exc:
                self.last_error = f"could not save the scan: {exc}"

    def mark_stale(self, reason: str) -> None:
        if self.result is not None:
            self.stale = True
            self.stale_reason = reason

    @property
    def password_hash(self) -> str:
        """A password from the environment wins, so compose can enforce one."""
        env = password_from_env()
        return hash_password(env) if env else self.cfg.server.password_hash

    @property
    def locked(self) -> bool:
        return bool(self.password_hash)

    @property
    def configured(self) -> bool:
        return bool(source_is_configured(self.cfg) and self.cfg.tmdb.api_key)


def _config_path() -> Path:
    if env := os.getenv("NOSTALGIA_CONFIG"):
        return Path(env)
    return PROJECT_ROOT / "config.yaml"


state = AppState(_config_path())
app = FastAPI(title="Nostalgia Line", version=__version__)


# -- models ---------------------------------------------------------------


class SettingsIn(BaseModel):
    source: str | None = None
    plex_url: str | None = None
    plex_token: str | None = None
    plex_libraries: list[str] | None = None
    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None
    jellyfin_libraries: list[str] | None = None
    nostalgiatv_m3u_url: str | None = None
    auto_refresh_logos: bool | None = None
    tmdb_api_key: str | None = None
    routing_mode: str | None = None
    multi_channel: str | None = None
    orphan_network: str | None = None
    include_movies: bool | None = None


class OverrideIn(BaseModel):
    uid: str
    channels: list[int] = Field(default_factory=list)


class BulkOverrideIn(BaseModel):
    uids: list[str] = Field(default_factory=list)
    channels: list[int] = Field(default_factory=list)
    mode: str = "replace"  # replace | add


class M3UImportIn(BaseModel):
    # Blank falls back to the saved playlist URL from Settings.
    url: str = ""


class NetworkMapIn(BaseModel):
    network: str
    channel: int


class StationIn(BaseModel):
    number: int | None = None
    name: str
    source_networks: list[str] = Field(default_factory=list)
    source_channels: list[int] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    mode: str = "claim"
    enabled: bool = True
    note: str = ""


class ExportIn(BaseModel):
    include_review: bool = False


# -- optional access control ----------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate /api behind a session when a password is set. Off by default."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/api") or path in OPEN_PATHS or not state.locked:
            return await call_next(request)
        if state.sessions.valid(request.cookies.get(COOKIE)):
            return await call_next(request)
        return JSONResponse({"detail": "authentication required"}, status_code=401)


app.add_middleware(AuthMiddleware)


class LoginIn(BaseModel):
    password: str


class PasswordIn(BaseModel):
    password: str = ""


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict:
    return {
        "enabled": state.locked,
        "authenticated": (not state.locked)
        or state.sessions.valid(request.cookies.get(COOKIE)),
        "enforced_by_env": bool(password_from_env()),
    }


@app.post("/api/auth/login")
def auth_login(payload: LoginIn) -> JSONResponse:
    if not state.locked:
        return JSONResponse({"authenticated": True, "note": "no password is set"})
    if not verify_password(payload.password, state.password_hash):
        raise HTTPException(status_code=401, detail="wrong password")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        COOKIE, state.sessions.issue(), httponly=True, samesite="lax", max_age=2592000
    )
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    state.sessions.revoke(request.cookies.get(COOKIE))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE)
    return response


@app.post("/api/auth/password")
def auth_set_password(payload: PasswordIn) -> dict:
    """Set or clear the password. Clearing leaves the app open, as it starts."""
    if password_from_env():
        raise HTTPException(
            status_code=400,
            detail="the password is set by NOSTALGIA_PASSWORD; change it there",
        )
    password = payload.password.strip()
    if password and len(password) < 6:
        raise HTTPException(status_code=400, detail="use at least 6 characters")
    state.cfg.server.password_hash = hash_password(password) if password else ""
    save_config(state.cfg, state.config_path)
    state.sessions.clear()
    return {"enabled": bool(password)}


# -- meta -----------------------------------------------------------------


@app.get("/api/status")
def status() -> dict:
    return {
        "version": __version__,
        "configured": state.configured,
        "source": state.cfg.source,
        "config_path": str(state.config_path),
        "scanning": bool(state.scan_task and not state.scan_task.done()),
        "progress": state.progress,
        "last_error": state.last_error,
        "last_export": state.last_export,
        "stale": state.stale,
        "stale_reason": state.stale_reason,
        "defaults": {
            "rows": len(state.defaults),
            "channels": len(state.catalog),
            **state.defaults.multi_channel_stats(),
        },
        "stations": len(state.stations),
        "store": state.store.stats(),
        "baseline": state.store.baseline,
        "last_export_at": state.store.last_export.get("at"),
        "pending": _pending_changes(),
        "posters": state.posters.stats(),
        "scan_at": state.result.finished_at if state.result else None,
        "stats": state.result.stats() if state.result else None,
        "diagnostics": state.result.diagnostics() if state.result else None,
    }


def _pending_changes() -> dict:
    """How far the current results have drifted from the loaded channels.csv."""
    if state.result is None:
        return {"additions": 0, "held_for_review": 0, "overrides": len(state.store.overrides)}
    rows, _, skipped = build_addition_rows(state.result, state.catalog, state.defaults)
    return {
        "additions": len(rows),
        "held_for_review": skipped,
        "overrides": len(state.store.overrides),
    }


@app.get("/api/workflow")
def workflow() -> dict:
    """Where the user is in the process, and what to do next."""
    pending = _pending_changes()
    diagnostics = state.result.diagnostics() if state.result else {}
    return build_workflow(
        configured=state.configured,
        source_name=state.cfg.source,
        scanned=state.result is not None,
        scan_stats=state.result.stats() if state.result else None,
        held_for_review=pending["held_for_review"],
        pending_additions=pending["additions"],
        last_export_at=state.store.last_export.get("at"),
        baseline_at=(state.store.baseline or {}).get("at"),
        lineup_rows=len(state.defaults),
        no_tmdb_id=diagnostics.get("no_tmdb_id", 0),
    ).to_dict()


@app.get("/api/settings")
def get_settings() -> dict:
    cfg = state.cfg
    return {
        "source": cfg.source,
        "plex_url": cfg.plex.url,
        "plex_token_set": bool(cfg.plex.token),
        "plex_libraries": cfg.plex.libraries,
        "jellyfin_url": cfg.jellyfin.url,
        "jellyfin_api_key_set": bool(cfg.jellyfin.api_key),
        "jellyfin_libraries": cfg.jellyfin.libraries,
        "nostalgiatv_m3u_url": cfg.nostalgiatv.m3u_url,
        "auto_refresh_logos": cfg.nostalgiatv.auto_refresh_logos,
        "tmdb_api_key_set": bool(cfg.tmdb.api_key),
        "routing_mode": cfg.routing.mode,
        "multi_channel": cfg.routing.multi_channel,
        "orphan_network": cfg.routing.orphan_network,
        "include_movies": cfg.routing.include_movies,
        "output": {"additions": cfg.output.additions_only, "merged": cfg.output.merged},
    }


@app.post("/api/settings")
def put_settings(payload: SettingsIn) -> dict:
    cfg = state.cfg
    if payload.source:
        if payload.source not in SOURCES:
            raise HTTPException(status_code=400, detail=f"source must be one of {SOURCES}")
        cfg.source = payload.source
    if payload.jellyfin_url is not None:
        cfg.jellyfin.url = payload.jellyfin_url.strip()
    if payload.jellyfin_api_key:
        cfg.jellyfin.api_key = payload.jellyfin_api_key.strip()
    if payload.jellyfin_libraries is not None:
        cfg.jellyfin.libraries = [s for s in payload.jellyfin_libraries if s.strip()]
    if payload.nostalgiatv_m3u_url is not None:
        cfg.nostalgiatv.m3u_url = payload.nostalgiatv_m3u_url.strip()
    if payload.auto_refresh_logos is not None:
        cfg.nostalgiatv.auto_refresh_logos = payload.auto_refresh_logos
    if payload.plex_url is not None:
        cfg.plex.url = payload.plex_url.strip()
    if payload.plex_token:
        cfg.plex.token = payload.plex_token.strip()
    if payload.plex_libraries is not None:
        cfg.plex.libraries = [s for s in payload.plex_libraries if s.strip()]
    if payload.tmdb_api_key:
        cfg.tmdb.api_key = payload.tmdb_api_key.strip()
    if payload.routing_mode:
        cfg.routing.mode = payload.routing_mode
    if payload.multi_channel:
        cfg.routing.multi_channel = payload.multi_channel
    if payload.orphan_network:
        cfg.routing.orphan_network = payload.orphan_network
    if payload.include_movies is not None:
        cfg.routing.include_movies = payload.include_movies
    try:
        cfg.routing.validate()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_config(cfg, state.config_path)
    state.mark_stale("routing settings changed")
    return get_settings()


@app.post("/api/test/server")
async def test_server() -> dict:
    """Test only the media server, so a bad TMDB key cannot mask a good Plex."""
    try:
        source = build_source(state.cfg)
        info = await source.ping()
        sections = await source.sections()
        usable = [s for s in sections if s.type in ("show", "movie")]
        return {
            "ok": True,
            "kind": source.name,
            "name": info.get("friendlyName") or source.name.title(),
            "version": info.get("version", ""),
            "sections": [{"title": s.title, "type": s.type} for s in usable],
            "detail": f"{len(usable)} usable librar{'y' if len(usable) == 1 else 'ies'}",
        }
    except SourceError as exc:
        return {"ok": False, "kind": state.cfg.source, "error": str(exc)}


@app.post("/api/test/tmdb")
async def test_tmdb() -> dict:
    try:
        cache = TMDBCache(state.cfg.path(state.cfg.data.cache_dir))
        await TMDBClient(state.cfg.tmdb.api_key, cache, state.cfg.tmdb.rate_limit).verify()
        cached = cache.stats()
        return {
            "ok": True,
            "cached": cached,
            "detail": f"{cached.get('series', 0)} series cached",
        }
    except TMDBError as exc:
        return {"ok": False, "error": str(exc)}


# -- scanning -------------------------------------------------------------


@app.post("/api/scan")
async def scan(include_movies: bool | None = None) -> dict:
    if state.scan_task and not state.scan_task.done():
        raise HTTPException(status_code=409, detail="a scan is already running")
    if not state.configured:
        raise HTTPException(
            status_code=400,
            detail=f"{missing_credential_message(state.cfg)}, and a TMDB key",
        )
    state.last_error = ""
    state.stale = False
    state.stale_reason = ""
    state.reload_reference_data()

    def progress(phase: str, done: int, total: int) -> None:
        state.progress = {"phase": phase, "done": done, "total": total}

    async def worker() -> None:
        try:
            state.result = await run_scan(
                state.cfg,
                state.catalog,
                state.defaults,
                state.stations,
                overrides=state.store.overrides,
                network_overrides=state.store.networks,
                include_movies=(
                    state.cfg.routing.include_movies if include_movies is None else include_movies
                ),
                progress=progress,
            )
            state.progress = {"phase": "done", "done": 1, "total": 1}
            state.persist_result()
            await _refresh_playlist_logos()
        except asyncio.CancelledError:
            state.progress = {"phase": "cancelled", "done": 0, "total": 0}
            state.last_error = "scan cancelled"
            raise
        except (SourceError, TMDBError) as exc:
            state.last_error = str(exc)
            state.progress = {"phase": "error", "done": 0, "total": 0}
        except Exception as exc:  # surfaced in the UI rather than lost to a log
            state.last_error = f"{type(exc).__name__}: {exc}"
            state.progress = {"phase": "error", "done": 0, "total": 0}

    state.scan_task = asyncio.create_task(worker())
    return {"started": True}


@app.post("/api/scan/cancel")
async def cancel_scan() -> dict:
    """Stop a running scan. A large library plus a cold TMDB cache takes a while."""
    task = state.scan_task
    if task is None or task.done():
        raise HTTPException(status_code=409, detail="no scan is running")
    task.cancel()
    state.progress = {"phase": "cancelled", "done": 0, "total": 0}
    state.last_error = "scan cancelled"
    return {"cancelled": True}


async def _refresh_playlist_logos() -> None:
    """Pull artwork after a scan, so a new custom channel gets its logo."""
    cfg = state.cfg.nostalgiatv
    if not (cfg.auto_refresh_logos and cfg.m3u_url.strip()):
        return
    try:
        importer = LogoImporter(
            state.catalog, state.cfg.path("logos"), _network_map_with_overrides()
        )
        await importer.import_from_m3u(cfg.m3u_url.strip())
    except Exception:  # artwork is cosmetic - never fail a scan over it
        pass


@app.get("/api/library")
def library(
    status_filter: str = "",
    section: str = "",
    q: str = "",
    channel: int | None = None,
    network: str = "",
    rule: str = "",
    confidence: str = "",
    source: str = "",
    item_type: str = "",
    review_only: bool = False,
    sort: str = "title",
    direction: str = "asc",
    offset: int = 0,
    limit: int = 200,
) -> dict:
    if state.result is None:
        return {"total": 0, "items": [], "sections": [], "scanned": False}

    entries = state.result.entries
    if status_filter:
        wanted = set(status_filter.split(","))
        entries = [e for e in entries if e.status in wanted]
    if section:
        entries = [e for e in entries if e.section == section]
    if review_only:
        entries = [e for e in entries if e.resolution.needs_review]
    if channel is not None:
        entries = [e for e in entries if channel in e.channels]
    if network:
        entries = [e for e in entries if (e.network or "") == network]
    if rule:
        wanted = set(rule.split(","))
        entries = [
            e for e in entries if any(a.rule in wanted for a in e.resolution.assignments)
        ]
    if confidence:
        wanted = set(confidence.split(","))
        entries = [e for e in entries if e.resolution.confidence in wanted]
    if source:
        wanted = set(source.split(","))
        entries = [e for e in entries if e.mapping_source in wanted]
    if item_type:
        entries = [e for e in entries if e.type == item_type]
    if q:
        needle = q.casefold()
        entries = [
            e
            for e in entries
            if needle in e.title.casefold() or needle in (e.network or "").casefold()
        ]

    def sort_key(entry):
        if sort == "year":
            return (entry.year is None, entry.year or 0)
        if sort == "episodes":
            return entry.episode_count
        if sort == "status":
            return entry.status
        if sort == "network":
            return (entry.network or "").casefold()
        if sort == "channel":
            channels = entry.channels
            return channels[0] if channels else 99999
        if sort == "confidence":
            from .cascade import CONFIDENCE_ORDER

            return (
                CONFIDENCE_ORDER.get(entry.resolution.confidence, 9),
                entry.title.casefold(),
            )
        if sort == "seasons":
            return entry.season_count
        return entry.title.casefold()

    entries = sorted(entries, key=sort_key, reverse=(direction == "desc"))
    total = len(entries)
    window = entries[offset : offset + max(1, min(limit, 1000))]
    return {
        "total": total,
        "offset": offset,
        "scanned": True,
        "sections": state.result.sections,
        "items": [e.to_dict(state.catalog) for e in window],
    }


@app.get("/api/item/{uid:path}")
def item(uid: str) -> dict:
    """One library entry. Backs the assign dialog without refetching the table."""
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")
    entry = next((e for e in state.result.entries if e.uid == uid), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no library item {uid}")
    return entry.to_dict(state.catalog)


@app.get("/api/channels")
def channels() -> dict:
    rollup = (
        state.result.channel_rollup(state.catalog, state.defaults)
        if state.result
        else [
            {
                "number": c.number,
                "name": c.name,
                "category": c.category,
                "accepts_content": c.accepts_content,
                "existing": len(state.defaults.titles_on_channel(c.number)),
                "added": 0,
                "total": len(state.defaults.titles_on_channel(c.number)),
                "empty": c.accepts_content and not state.defaults.titles_on_channel(c.number),
                "thin": False,
            }
            for c in state.catalog
        ]
    )
    importer = LogoImporter(state.catalog, state.cfg.path("logos"), _network_map_with_overrides())
    installed = importer.installed()
    mounted: dict[int, str] = {}
    for directory in _extra_logo_dirs():
        mounted.update(
            LogoImporter(state.catalog, directory, _network_map_with_overrides()).installed()
        )
    for row in rollup:
        number = row["number"]
        if number in installed or number in mounted:
            row["logo_source"] = "file"
        elif _tmdb_logo_for_channel(number):
            row["logo_source"] = "tmdb"
        else:
            row["logo_source"] = "badge"
    return {"channels": rollup}


@app.get("/api/poster")
async def poster(path: str, size: str = DEFAULT_SIZE):
    """Serve a TMDB poster from disk, fetching it once on the first request.

    Cached under /config/cache/posters. TMDB poster paths are content-addressed,
    so the response is safe to cache hard in the browser as well.
    """
    if not PosterCache.valid(path):
        raise HTTPException(status_code=400, detail="not a TMDB poster path")
    local = await state.posters.fetch(path, size)
    if local is None:
        raise HTTPException(status_code=404, detail="poster unavailable")
    return FileResponse(
        local,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )


@app.post("/api/posters/clear")
def clear_posters() -> dict:
    return {"removed": state.posters.clear()}


@app.get("/api/logos")
def list_logos() -> dict:
    """What artwork is installed, and which channels are still on a badge."""
    importer = LogoImporter(state.catalog, state.cfg.path("logos"), _network_map_with_overrides())
    all_installed = importer.installed()
    routable = [c for c in state.catalog if c.accepts_content]
    routable_numbers = {c.number for c in routable}
    # Count only routable channels, or the three figures do not sum to the total.
    installed = {n: f for n, f in all_installed.items() if n in routable_numbers}
    return {
        "directory": str(state.cfg.path("logos")),
        "installed": [
            {"channel": n, "name": state.catalog.name_of(n), "file": f}
            for n, f in sorted(installed.items())
        ],
        "installed_count": len(installed),
        "from_tmdb": sum(
            1 for c in routable if c.number not in installed and _tmdb_logo_for_channel(c.number)
        ),
        "missing_count": sum(
            1
            for c in routable
            if c.number not in installed and not _tmdb_logo_for_channel(c.number)
        ),
        "total_channels": len(routable),
        "extra_dirs": [str(d) for d in _extra_logo_dirs()],
    }


@app.post("/api/logos")
async def import_logos(files: list[UploadFile] = File(...)) -> dict:
    """Import channel artwork.

    Accepts any number of images, or a zip of them. Filenames are matched to
    channels by number, by channel name, or by NostalgiaTV's own
    `logo_<name>.png` convention, so a folder lifted straight out of another
    install imports without renaming anything.
    """
    importer = LogoImporter(state.catalog, state.cfg.path("logos"), _network_map_with_overrides())
    report = None
    plain: list[tuple[str, bytes]] = []

    def merge(into, other):
        if into is None:
            return other
        into.imported += other.imported
        into.unmatched += other.unmatched
        into.skipped += other.skipped
        return into

    for upload in files:
        blob = await upload.read()
        name = upload.filename or "unnamed"
        if name.lower().endswith((".m3u", ".m3u8")):
            # A playlist names the artwork rather than containing it, so the
            # logo URLs inside still have to be reachable from here.
            report = merge(
                report,
                await importer.import_from_m3u_text(blob.decode("utf-8", errors="replace")),
            )
        elif name.lower().endswith(".zip"):
            report = merge(report, importer.import_zip(blob))
        else:
            plain.append((name, blob))

    if plain:
        report = merge(report, importer.import_files(plain))

    if report is None:
        raise HTTPException(status_code=400, detail="no files uploaded")
    return report.to_dict()


@app.post("/api/logos/from-m3u")
async def import_logos_from_m3u(payload: M3UImportIn) -> dict:
    """Pull every channel logo from an M3U playlist in one go."""
    importer = LogoImporter(
        state.catalog, state.cfg.path("logos"), _network_map_with_overrides()
    )
    url = payload.url.strip() or state.cfg.nostalgiatv.m3u_url.strip()
    if not url:
        raise HTTPException(
            status_code=400,
            detail="no playlist URL - set one on the Settings tab, or pass one here",
        )
    report = await importer.import_from_m3u(url)
    # Remember a URL that worked, so it becomes a one-time setup step.
    if report.imported and not state.cfg.nostalgiatv.m3u_url:
        state.cfg.nostalgiatv.m3u_url = url
        save_config(state.cfg, state.config_path)
    return report.to_dict()


@app.delete("/api/logos")
def clear_logos() -> dict:
    importer = LogoImporter(state.catalog, state.cfg.path("logos"), _network_map_with_overrides())
    return {"removed": importer.clear()}


@app.get("/api/station-logo")
async def station_logo(network: str):
    """The real station's own logo, from TMDB.

    Harvested during the scan from the same payload that gives us the network
    name, so it costs nothing extra. Served through the poster cache.
    """
    if state.result is None:
        raise HTTPException(status_code=404, detail="run a scan first")
    logo_path = state.result.network_logos.get(network)
    if not logo_path:
        # Try case-insensitively; TMDB is not always consistent about casing.
        folded = network.casefold()
        logo_path = next(
            (p for n, p in state.result.network_logos.items() if n.casefold() == folded),
            None,
        )
    if not logo_path:
        raise HTTPException(status_code=404, detail=f"no logo for {network}")
    local = await state.posters.fetch(logo_path, "w154")
    if local is None:
        raise HTTPException(status_code=404, detail="logo unavailable")
    return FileResponse(
        local,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/api/channel-logo/{number}")
async def channel_logo(number: int):
    """A channel's logo.

    Looks in /config/logos for a file named after the channel number, or after
    its name in NostalgiaTV's own `logo_<name>.png` convention - so mounting an
    existing logo folder read-only just works. Falls back to a generated badge,
    which means the UI always has something to show.
    """
    channel = state.catalog.get(number)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"no channel {number}")

    # Use the importer's matcher rather than a second, weaker lookup. Artwork
    # copied straight into /config/logos keeps its original filename, which is
    # very often the real network - logo_tnt.png for T.N.Tea - and a name-only
    # match misses those. One matcher, so listing and serving cannot disagree.
    importer = LogoImporter(
        state.catalog, state.cfg.path("logos"), _network_map_with_overrides()
    )
    filename = importer.installed().get(number)
    if filename:
        candidate = state.cfg.path("logos") / filename
        if candidate.exists():
            return FileResponse(
                candidate, headers={"Cache-Control": "public, max-age=86400"}
            )

    # 2. Artwork from a read-only mount, matched the same way.
    for directory in _extra_logo_dirs():
        mounted = LogoImporter(state.catalog, directory, _network_map_with_overrides())
        name = mounted.installed().get(number)
        if name and (directory / name).exists():
            return FileResponse(
                directory / name, headers={"Cache-Control": "public, max-age=86400"}
            )

    # 3. The real network's logo from TMDB, cached on disk like a poster.
    logo_path = _tmdb_logo_for_channel(number)
    if logo_path:
        local = await state.posters.fetch(logo_path, "w154")
        if local is not None:
            return FileResponse(
                local,
                media_type="image/png",
                headers={
                    "Cache-Control": "public, max-age=604800",
                    "X-Logo-Source": "tmdb",
                },
            )

    return Response(
        content=_logo_placeholder(channel),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _extra_logo_dirs() -> list[Path]:
    """Optional read-only logo mounts, highest priority first.

    Lets a compose file point at an existing artwork folder
    (``- /path/to/logos:/logos:ro``) without copying anything in.
    """
    dirs = []
    # os.pathsep, not ":" - a literal colon swallows the drive letter on Windows.
    for raw in (os.getenv("NOSTALGIA_LOGO_DIRS") or "/logos").split(os.pathsep):
        candidate = Path(raw.strip())
        if raw.strip() and candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _tmdb_logo_for_channel(number: int) -> str | None:
    """The TMDB logo of a real network this channel stands in for.

    Every scan already downloads each series' ``networks[]``, which carries a
    ``logo_path``, so real artwork is available for most channels without any
    configuration and without a single extra API call.
    """
    if state.result is None or not state.result.network_logos:
        return None
    network_map = _network_map_with_overrides()
    # Prefer the network whose name is closest to the channel's own billing.
    candidates = []
    for network, logo_path in state.result.network_logos.items():
        mapped = network_map.get(network)
        if mapped and mapped[0] == number:
            candidates.append((len(network), network, logo_path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


# Deterministic hue per channel so a logo-less lineup still reads as distinct.
_LOGO_PALETTE = [
    ("#1f3a5f", "#8ab4f8"), ("#3b2f5e", "#c5a3ff"), ("#14453d", "#5fd0bc"),
    ("#5a3320", "#ffab70"), ("#4a1f3d", "#ff9ecb"), ("#1e4620", "#8fd694"),
    ("#4a4520", "#e8d16b"), ("#402020", "#ff9b9b"),
]


def _logo_placeholder(channel) -> str:
    bg, fg = _LOGO_PALETTE[channel.number % len(_LOGO_PALETTE)]
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", channel.name))[:3].upper()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 40" width="64" height="40">'
        f'<rect width="64" height="40" rx="6" fill="{bg}"/>'
        f'<text x="32" y="19" font-family="ui-monospace,monospace" font-size="15" font-weight="700"'
        f' fill="{fg}" text-anchor="middle">{initials}</text>'
        f'<text x="32" y="32" font-family="ui-monospace,monospace" font-size="9"'
        f' fill="{fg}" fill-opacity="0.75" text-anchor="middle">{channel.number}</text>'
        f"</svg>"
    )


@app.get("/api/channels/{number}/titles")
def channel_titles(number: int) -> dict:
    """Everything sitting on one channel, from your library and from the lineup file."""
    channel = state.catalog.get(number)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"no channel {number}")

    in_library = state.result.channel_titles(number, state.catalog) if state.result else []
    known = {(normalize_title(r["title"]), r["year"]) for r in in_library}

    # Rows the lineup file puts here for titles you do not actually have. Worth
    # showing: they explain a channel's count, but there is nothing to edit.
    absent = [
        {"title": row.title, "year": row.release_year}
        for row in state.defaults.titles_on_channel(number)
        if (normalize_title(row.title), row.release_year) not in known
    ]
    absent.sort(key=lambda r: r["title"].casefold())

    return {
        "channel": {"number": channel.number, "name": channel.name, "category": channel.category},
        "titles": in_library,
        "not_in_library": absent,
        "counts": {
            "in_library": len(in_library),
            "not_in_library": len(absent),
            "needs_review": sum(1 for r in in_library if r["needs_review"]),
        },
    }


@app.get("/api/channels/{number}/network-catalog")
async def channel_network_catalog(number: int, pages: int = 2) -> dict:
    """What actually aired on the real network this channel stands in for.

    Answers the question the channel view cannot: not just what you have, but
    what the station itself ran - and therefore what you are missing.
    """
    channel = state.catalog.get(number)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"no channel {number}")
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")

    network_map = _network_map_with_overrides()
    networks = [
        (name, nid)
        for name, nid in state.result.network_ids.items()
        if (mapped := network_map.get(name)) and mapped[0] == number
    ]
    if not networks:
        return {
            "channel": {"number": channel.number, "name": channel.name},
            "networks": [],
            "titles": [],
            "note": (
                "No TMDB network in your library maps to this channel, so there is "
                "no real-world station to list."
            ),
        }

    # Several networks can map to one channel. Pick the one that actually
    # accounts for the most of your titles here - shortest-name lost badly,
    # handing Cartoon Net the catalogue of YTV because "YTV" is three letters.
    weight: dict[str, int] = {}
    for entry in state.result.entries:
        if entry.network and number in entry.channels:
            weight[entry.network] = weight.get(entry.network, 0) + 1
    name, network_id = max(networks, key=lambda pair: (weight.get(pair[0], 0), -len(pair[0])))
    try:
        cache = TMDBCache(state.cfg.path(state.cfg.data.cache_dir))
        client = TMDBClient(state.cfg.tmdb.api_key, cache, state.cfg.tmdb.rate_limit)
        catalog = await client.network_catalog(network_id, pages=pages)
    except TMDBError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    mine = {e.tmdb_id: e for e in state.result.entries if e.tmdb_id}
    titles = []
    for show in catalog:
        entry = mine.get(show["tmdb_id"])
        titles.append(
            {
                **show,
                "in_library": entry is not None,
                "uid": entry.uid if entry else None,
                "channels": (
                    [{"number": n, "name": state.catalog.name_of(n)} for n in entry.channels]
                    if entry
                    else []
                ),
                "elsewhere": bool(entry and number not in entry.channels),
            }
        )
    have = sum(1 for t in titles if t["in_library"])
    return {
        "channel": {"number": channel.number, "name": channel.name},
        "networks": [n for n, _ in networks],
        "network": name,
        "titles": titles,
        "counts": {"total": len(titles), "in_library": have, "missing": len(titles) - have},
    }


@app.get("/api/review")
def review() -> dict:
    if state.result is None:
        return {"total": 0, "items": []}
    queue = [e for e in state.result.review_queue() if e.uid not in state.store.dismissed]
    return {
        "total": len(queue),
        "items": [e.to_dict(state.catalog) for e in queue],
    }


@app.post("/api/override")
def override(payload: OverrideIn) -> dict:
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")
    unknown = [c for c in payload.channels if state.catalog.get(c) is None]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown channels: {unknown}")
    entry = next((e for e in state.result.entries if e.uid == payload.uid), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no library item {payload.uid}")
    state.store.set_override(payload.uid, payload.channels)
    apply_override(entry, payload.channels, state.catalog)
    state.persist_result()
    return entry.to_dict(state.catalog)


@app.delete("/api/override/{uid}")
def clear_override(uid: str) -> dict:
    state.store.clear_override(uid)
    return {"cleared": uid, "note": "re-scan to restore the cascade result"}


@app.post("/api/override/bulk")
def override_bulk(payload: BulkOverrideIn) -> dict:
    """Assign many titles at once - the whole point of the media-library view."""
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")
    if payload.mode not in ("replace", "add"):
        raise HTTPException(status_code=400, detail="mode must be 'replace' or 'add'")
    unknown = [c for c in payload.channels if state.catalog.get(c) is None]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown channels: {unknown}")

    by_uid = {e.uid: e for e in state.result.entries}
    missing = [u for u in payload.uids if u not in by_uid]
    if missing:
        raise HTTPException(status_code=404, detail=f"{len(missing)} unknown items")

    for uid in payload.uids:
        entry = by_uid[uid]
        if payload.mode == "add":
            channels = sorted({*entry.channels, *payload.channels})
        else:
            channels = list(payload.channels)
        state.store.overrides[uid] = channels
        apply_override(entry, channels, state.catalog)
    state.store.save()
    state.persist_result()
    return {"updated": len(payload.uids), "channels": payload.channels, "mode": payload.mode}


@app.post("/api/dismiss/{uid}")
def dismiss(uid: str) -> dict:
    state.store.dismiss(uid)
    return {"dismissed": uid}


# -- networks -------------------------------------------------------------


def _network_map_with_overrides():
    network_map = load_network_map(state.cfg.path(state.cfg.data.network_map))
    network_map.apply_overrides(state.store.networks, state.catalog)
    return network_map


@app.get("/api/networks")
def networks() -> dict:
    """Every network in the library, worst-covered first."""
    if state.result is None:
        return {"total": 0, "networks": [], "scanned": False, "diagnostics": None}
    network_map = _network_map_with_overrides()
    orphan_map = load_orphan_networks(state.cfg.path(state.cfg.data.orphan_networks))
    rows = state.result.network_rollup(network_map, orphan_map, state.catalog)
    return {
        "total": len(rows),
        "scanned": True,
        "networks": rows,
        "diagnostics": state.result.diagnostics(),
        "unmapped_titles": sum(r["titles"] for r in rows if r["status"] == "unmapped"),
    }


@app.post("/api/networks/map")
def map_network(payload: NetworkMapIn) -> dict:
    channel = state.catalog.get(payload.channel)
    if channel is None:
        raise HTTPException(status_code=400, detail=f"unknown channel {payload.channel}")
    if not channel.accepts_content:
        raise HTTPException(
            status_code=400,
            detail=f"'{channel.name}' holds no content and cannot be a routing target",
        )
    if not payload.network.strip():
        raise HTTPException(status_code=400, detail="network may not be blank")
    state.store.map_network(payload.network, channel.number)
    state.mark_stale(f"'{payload.network}' remapped")
    return {
        "network": payload.network,
        "channel": channel.number,
        "channel_name": channel.name,
        "note": "re-scan to apply this to the library",
    }


@app.delete("/api/networks/map/{network:path}")
def unmap_network(network: str) -> dict:
    if not state.store.unmap_network(network):
        raise HTTPException(status_code=404, detail=f"'{network}' is not custom-mapped")
    state.mark_stale(f"'{network}' mapping cleared")
    return {"unmapped": network}


# -- custom stations ------------------------------------------------------


@app.get("/api/stations")
def get_stations() -> dict:
    return {
        "stations": [s.to_dict() for s in state.stations],
        "problems": state.stations.validate_against(state.catalog),
        "next_number": state.stations.next_number(),
        "band_start": CUSTOM_BAND_START,
        "known_networks": sorted(
            {n for n in load_network_map(state.cfg.path(state.cfg.data.network_map))}
            | {n for n in load_orphan_networks(state.cfg.path(state.cfg.data.orphan_networks))}
        ),
    }


@app.post("/api/stations")
def put_station(payload: StationIn) -> dict:
    number = payload.number or state.stations.next_number()
    stock = state.catalog.get(number)
    if stock is not None and stock.category != "custom":
        raise HTTPException(
            status_code=400,
            detail=f"channel {number} is the stock channel '{stock.name}'. Pick another number.",
        )
    try:
        station = CustomStation(
            number=number,
            name=payload.name,
            source_networks=payload.source_networks,
            source_channels=payload.source_channels,
            keywords=payload.keywords,
            mode=payload.mode,
            enabled=payload.enabled,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.stations.upsert(station)
    state.stations.save(state.cfg.path("stations.json"))
    state.stations.register_with(state.catalog)
    state.mark_stale(f"custom station '{station.name}' changed")
    return station.to_dict()


@app.delete("/api/stations/{number}")
def delete_station(number: int) -> dict:
    if not state.stations.remove(number):
        raise HTTPException(status_code=404, detail=f"no custom station {number}")
    state.stations.save(state.cfg.path("stations.json"))
    state.catalog.remove(number)
    state.mark_stale(f"custom station {number} deleted")
    return {"deleted": number}


# -- export ---------------------------------------------------------------


@app.get("/api/export/preview")
def export_preview(include_review: bool = False) -> dict:
    """What an export would write, without writing it."""
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")
    rows, secondary, skipped = build_addition_rows(
        state.result, state.catalog, state.defaults, include_review=include_review
    )
    per_channel: dict[int, int] = {}
    for row in rows:
        per_channel[row.channel_number] = per_channel.get(row.channel_number, 0) + 1
    return {
        "additions": len(rows),
        "secondary_rows": secondary,
        "skipped_review": skipped,
        "original_rows": len(state.defaults),
        "merged_rows": len(state.defaults) + len(rows),
        "include_review": include_review,
        "top_channels": [
            {"number": n, "name": state.catalog.name_of(n), "rows": c}
            for n, c in sorted(per_channel.items(), key=lambda kv: -kv[1])[:10]
        ],
        "sample": [
            {
                "channel_number": r.channel_number,
                "channel_name": r.channel_name,
                "title": r.title,
                "release_year": r.release_year,
            }
            for r in rows[:10]
        ],
    }


@app.get("/api/export/preflight")
def export_preflight(include_review: bool = False) -> dict:
    """Would this import cleanly? Checked before anything is written."""
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")
    return run_preflight(state.result, state.catalog, state.defaults, include_review)


@app.post("/api/export")
def export_csv(payload: ExportIn) -> dict:
    if state.result is None:
        raise HTTPException(status_code=400, detail="run a scan first")
    report = run_export(
        state.result,
        state.catalog,
        state.defaults,
        state.cfg.path(state.cfg.output.additions_only),
        state.cfg.path(state.cfg.output.merged),
        include_review=payload.include_review,
    )
    state.last_export = report.to_dict()
    state.store.record_export(state.last_export)
    return state.last_export


@app.post("/api/channels-file")
async def upload_channels_file(request: Request) -> dict:
    """Replace the default assignments with the user's own NostalgiaTV export.

    Sent as a raw text/csv body so the app needs no multipart dependency. The
    incoming file is fully validated before anything on disk is touched, and the
    file being replaced is backed up first.
    """
    raw = (await request.body()).decode("utf-8-sig", errors="replace")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="empty upload")

    target = state.cfg.path(state.cfg.data.channels_csv)
    scratch = target.with_suffix(".incoming")
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(raw, encoding="utf-8")
    try:
        candidate = DefaultAssignments.load(scratch)
    except (ValueError, KeyError) as exc:
        scratch.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"not a valid channels.csv: {exc}") from exc
    if not len(candidate):
        scratch.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="that file has a header but no rows")

    previous = len(state.defaults)
    backup = ""
    if target.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = target.with_name(f"{target.stem}.{stamp}.bak{target.suffix}")
        backup_path.write_bytes(target.read_bytes())
        backup = str(backup_path)
    scratch.replace(target)

    state.defaults = DefaultAssignments.load(target)
    state.store.record_baseline(
        rows=len(state.defaults),
        channels=len({r.channel_number for r in state.defaults.rows}),
        digest=hashlib.sha256(target.read_bytes()).hexdigest()[:16],
        filename="channels.csv",
    )
    state.result = None  # the old scan was diffed against the old file
    state.scan_path.unlink(missing_ok=True)
    return {
        "rows": len(state.defaults),
        "previous_rows": previous,
        "channels": len({r.channel_number for r in state.defaults.rows}),
        "backup": backup,
        "note": "re-scan to diff your library against the new file",
        **state.defaults.multi_channel_stats(),
    }


@app.get("/api/download/{which}")
def download(which: str):
    if which not in ("additions", "merged"):
        raise HTTPException(status_code=404, detail="unknown file")
    name = (
        state.cfg.output.additions_only if which == "additions" else state.cfg.output.merged
    )
    path = state.cfg.path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="export it first")
    return FileResponse(path, media_type="text/csv", filename=Path(name).name)


# -- static ---------------------------------------------------------------


def _asset_version() -> str:
    """A token that changes whenever the UI files do.

    index.html is served with no-store and the CSS/JS carry this in their query
    string, so an upgrade always lands. Without it the browser keeps serving the
    old interface and the new one appears simply not to exist.
    """
    stamp = 0.0
    for name in ("index.html", "app.js", "styles.css"):
        candidate = WEB_DIR / name
        if candidate.exists():
            stamp = max(stamp, candidate.stat().st_mtime)
    return f"{__version__}-{int(stamp)}"


@app.get("/", include_in_schema=False)
def index() -> Response:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    version = _asset_version()
    html = html.replace('href="styles.css"', f'href="styles.css?v={version}"')
    html = html.replace('src="app.js"', f'src="app.js?v={version}"')
    return Response(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:  # pragma: no cover

    @app.get("/")
    def missing_ui() -> JSONResponse:
        return JSONResponse({"error": f"web assets not found at {WEB_DIR}"}, status_code=500)
