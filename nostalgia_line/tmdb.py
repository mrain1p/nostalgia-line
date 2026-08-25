"""TMDB client (spec S2, S13.2).

Libraries are large and mostly static, so everything is cached on disk by tmdb_id
and reused across runs. The only field that matters for series routing lives in
``networks[]``; movies have no such field at all.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

API_ROOT = "https://api.themoviedb.org/3"


class TMDBError(RuntimeError):
    """TMDB rejected the request or was unreachable."""


@dataclass
class TMDBSeries:
    tmdb_id: int
    name: str = ""
    networks: list[str] = field(default_factory=list)
    # network name -> TMDB logo path. Free: the /tv payload already carries it,
    # so channel artwork costs no extra requests.
    network_logos: dict[str, str] = field(default_factory=dict)
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    first_air_date: str = ""
    origin_country: list[str] = field(default_factory=list)
    original_language: str = ""
    overview: str = ""
    poster_path: str = ""
    in_production: bool = False
    episode_count: int = 0
    external_ids: dict[str, str] = field(default_factory=dict)
    missing: bool = False

    @property
    def year(self) -> int | None:
        head = self.first_air_date[:4]
        return int(head) if head.isdigit() else None

    @property
    def primary_network(self) -> str | None:
        return self.networks[0] if self.networks else None

    def to_dict(self) -> dict:
        return {
            "tmdb_id": self.tmdb_id,
            "name": self.name,
            "networks": self.networks,
            "network_logos": self.network_logos,
            "genres": self.genres,
            "keywords": self.keywords,
            "first_air_date": self.first_air_date,
            "origin_country": self.origin_country,
            "original_language": self.original_language,
            "overview": self.overview,
            "poster_path": self.poster_path,
            "in_production": self.in_production,
            "episode_count": self.episode_count,
            "external_ids": self.external_ids,
            "missing": self.missing,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TMDBSeries":
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


@dataclass
class TMDBMovie:
    """Post-1.0. Films route on genre, decade and collection only (spec S3)."""

    tmdb_id: int
    title: str = ""
    genres: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    release_date: str = ""
    original_language: str = ""
    production_companies: list[str] = field(default_factory=list)
    collection: str = ""
    overview: str = ""
    poster_path: str = ""
    vote_average: float = 0.0
    vote_count: int = 0
    missing: bool = False

    @property
    def year(self) -> int | None:
        head = self.release_date[:4]
        return int(head) if head.isdigit() else None

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, raw: dict) -> "TMDBMovie":
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


class _RateLimiter:
    """Simple token-per-second gate. TMDB documents a 50/sec ceiling."""

    def __init__(self, per_second: int):
        self.per_second = max(1, per_second)
        self._lock = asyncio.Lock()
        self._window_start = 0.0
        self._count = 0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 1.0:
                self._window_start = now
                self._count = 0
            if self._count >= self.per_second:
                await asyncio.sleep(max(0.0, 1.0 - (now - self._window_start)))
                self._window_start = time.monotonic()
                self._count = 0
            self._count += 1


class TMDBCache:
    """One JSON file per kind. Small enough to hold in memory, cheap to reload."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, dict]] = {}
        self._dirty: set[str] = set()

    def _path(self, kind: str) -> Path:
        return self.dir / f"tmdb_{kind}.json"

    def _bucket(self, kind: str) -> dict[str, dict]:
        if kind not in self._data:
            path = self._path(kind)
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as fh:
                        self._data[kind] = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    self._data[kind] = {}
            else:
                self._data[kind] = {}
        return self._data[kind]

    def get(self, kind: str, tmdb_id: int) -> dict | None:
        return self._bucket(kind).get(str(tmdb_id))

    def put(self, kind: str, tmdb_id: int, payload: dict) -> None:
        self._bucket(kind)[str(tmdb_id)] = payload
        self._dirty.add(kind)

    def flush(self) -> None:
        for kind in list(self._dirty):
            path = self._path(kind)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data[kind], fh)
            tmp.replace(path)
        self._dirty.clear()

    def clear(self) -> None:
        for path in self.dir.glob("tmdb_*.json"):
            path.unlink(missing_ok=True)
        self._data.clear()
        self._dirty.clear()

    def stats(self) -> dict[str, int]:
        return {kind: len(self._bucket(kind)) for kind in ("series", "movie")}


class TMDBClient:
    def __init__(
        self,
        api_key: str,
        cache: TMDBCache,
        rate_limit: int = 50,
        concurrency: int = 16,
        timeout: float = 20.0,
    ):
        if not api_key:
            raise TMDBError("tmdb.api_key is not configured")
        self.api_key = api_key
        self.cache = cache
        self.timeout = timeout
        self._limiter = _RateLimiter(rate_limit)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    # -- transport -------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> dict | None:
        """One GET. Returns None for 404 so a missing id is data, not an exception."""
        params = {"api_key": self.api_key, **params}
        for attempt in range(4):
            await self._limiter.acquire()
            async with self._semaphore:
                try:
                    response = await client.get(f"{API_ROOT}{path}", params=params)
                except httpx.HTTPError as exc:
                    if attempt == 3:
                        raise TMDBError(f"TMDB unreachable for {path}: {exc}") from exc
                    await asyncio.sleep(2**attempt * 0.5)
                    continue
            if response.status_code == 404:
                return None
            if response.status_code == 401:
                raise TMDBError("TMDB rejected the api key (401). Check tmdb.api_key.")
            if response.status_code == 429:
                await asyncio.sleep(float(response.headers.get("Retry-After", "1")) + 0.25)
                continue
            if response.status_code >= 500:
                await asyncio.sleep(2**attempt * 0.5)
                continue
            if response.status_code >= 400:
                raise TMDBError(f"TMDB returned {response.status_code} for {path}")
            return response.json()
        raise TMDBError(f"TMDB kept failing for {path}")

    async def verify(self) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            await self._get(client, "/configuration")
        return True

    # -- series ----------------------------------------------------------

    async def _fetch_series(self, client: httpx.AsyncClient, tmdb_id: int) -> TMDBSeries:
        payload = await self._get(
            client, f"/tv/{tmdb_id}", append_to_response="keywords,external_ids"
        )
        if payload is None:
            return TMDBSeries(tmdb_id=tmdb_id, missing=True)
        keywords = payload.get("keywords") or {}
        # /tv uses "results"; the appended block occasionally uses "keywords".
        keyword_rows = keywords.get("results") or keywords.get("keywords") or []
        external = payload.get("external_ids") or {}
        return TMDBSeries(
            tmdb_id=tmdb_id,
            name=payload.get("name") or "",
            networks=[n.get("name", "") for n in (payload.get("networks") or []) if n.get("name")],
            network_logos={
                n["name"]: n["logo_path"]
                for n in (payload.get("networks") or [])
                if n.get("name") and n.get("logo_path")
            },
            genres=[g.get("name", "") for g in (payload.get("genres") or []) if g.get("name")],
            keywords=[k.get("name", "").lower() for k in keyword_rows if k.get("name")],
            first_air_date=payload.get("first_air_date") or "",
            origin_country=list(payload.get("origin_country") or []),
            original_language=payload.get("original_language") or "",
            overview=payload.get("overview") or "",
            poster_path=payload.get("poster_path") or "",
            in_production=bool(payload.get("in_production")),
            episode_count=int(payload.get("number_of_episodes") or 0),
            external_ids={
                k: str(v)
                for k, v in external.items()
                if v and k in ("tvdb_id", "imdb_id", "tvrage_id")
            },
        )

    async def series(self, tmdb_ids: list[int], progress=None) -> dict[int, TMDBSeries]:
        """Batch-resolve series, serving from cache wherever possible."""
        out: dict[int, TMDBSeries] = {}
        pending: list[int] = []
        for tmdb_id in dict.fromkeys(tmdb_ids):
            cached = self.cache.get("series", tmdb_id)
            if cached is not None:
                out[tmdb_id] = TMDBSeries.from_dict(cached)
            else:
                pending.append(tmdb_id)

        if not pending:
            if progress:
                progress(len(out), len(out))
            return out

        done = len(out)
        total = len(out) + len(pending)
        async with httpx.AsyncClient(timeout=self.timeout) as client:

            async def one(tmdb_id: int) -> None:
                nonlocal done
                series = await self._fetch_series(client, tmdb_id)
                self.cache.put("series", tmdb_id, series.to_dict())
                out[tmdb_id] = series
                done += 1
                if done % 250 == 0:
                    self.cache.flush()
                if progress and done % 10 == 0:
                    progress(done, total)

            await asyncio.gather(*(one(i) for i in pending))
        self.cache.flush()
        if progress:
            progress(done, total)
        return out

    # -- movies (post-1.0) -----------------------------------------------

    async def _fetch_movie(self, client: httpx.AsyncClient, tmdb_id: int) -> TMDBMovie:
        payload = await self._get(client, f"/movie/{tmdb_id}", append_to_response="keywords")
        if payload is None:
            return TMDBMovie(tmdb_id=tmdb_id, missing=True)
        keywords = payload.get("keywords") or {}
        keyword_rows = keywords.get("keywords") or keywords.get("results") or []
        collection = payload.get("belongs_to_collection") or {}
        return TMDBMovie(
            tmdb_id=tmdb_id,
            title=payload.get("title") or "",
            genres=[g.get("name", "") for g in (payload.get("genres") or []) if g.get("name")],
            keywords=[k.get("name", "").lower() for k in keyword_rows if k.get("name")],
            release_date=payload.get("release_date") or "",
            original_language=payload.get("original_language") or "",
            production_companies=[
                c.get("name", "") for c in (payload.get("production_companies") or []) if c.get("name")
            ],
            collection=(collection or {}).get("name", "") if isinstance(collection, dict) else "",
            overview=payload.get("overview") or "",
            poster_path=payload.get("poster_path") or "",
            vote_average=float(payload.get("vote_average") or 0.0),
            vote_count=int(payload.get("vote_count") or 0),
        )

    async def movies(self, tmdb_ids: list[int], progress=None) -> dict[int, TMDBMovie]:
        out: dict[int, TMDBMovie] = {}
        pending: list[int] = []
        for tmdb_id in dict.fromkeys(tmdb_ids):
            cached = self.cache.get("movie", tmdb_id)
            if cached is not None:
                out[tmdb_id] = TMDBMovie.from_dict(cached)
            else:
                pending.append(tmdb_id)
        if not pending:
            return out
        done = len(out)
        total = len(out) + len(pending)
        async with httpx.AsyncClient(timeout=self.timeout) as client:

            async def one(tmdb_id: int) -> None:
                nonlocal done
                movie = await self._fetch_movie(client, tmdb_id)
                self.cache.put("movie", tmdb_id, movie.to_dict())
                out[tmdb_id] = movie
                done += 1
                if done % 250 == 0:
                    self.cache.flush()
                if progress and done % 10 == 0:
                    progress(done, total)

            await asyncio.gather(*(one(i) for i in pending))
        self.cache.flush()
        return out
