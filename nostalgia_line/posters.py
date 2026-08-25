"""Poster cache.

Posters are fetched once, written to disk, and then served locally. Pointing 800
table rows straight at image.tmdb.org would mean 800 cold round trips on every
page, and TMDB would rightly rate-limit it. Cached files are immutable — a TMDB
poster path already contains a content hash — so they are served with a long
max-age and the browser stops asking too.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx

IMAGE_ROOT = "https://image.tmdb.org/t/p"
DEFAULT_SIZE = "w185"
ALLOWED_SIZES = ("w92", "w154", "w185", "w342", "w500")

# TMDB poster paths look like /wZ4ycT0Aq6BjJUXwXsQAJDbHDLc.jpg
_POSTER_PATH = re.compile(r"^/[A-Za-z0-9._-]+\.(jpg|jpeg|png|webp)$", re.IGNORECASE)


class PosterCache:
    def __init__(self, directory: str | Path, timeout: float = 20.0):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        # One in-flight fetch per poster: a table paints many rows at once and
        # would otherwise start the same download several times over.
        self._locks: dict[str, asyncio.Lock] = {}

    def _safe_name(self, poster_path: str, size: str) -> str:
        return f"{size}_{poster_path.lstrip('/')}"

    def path_for(self, poster_path: str, size: str = DEFAULT_SIZE) -> Path:
        return self.dir / self._safe_name(poster_path, size)

    @staticmethod
    def valid(poster_path: str) -> bool:
        """Reject anything that is not a TMDB poster path.

        The value reaches us from a query string, so it must never be allowed to
        walk out of the cache directory or address an arbitrary host.
        """
        return bool(poster_path) and bool(_POSTER_PATH.match(poster_path))

    async def fetch(self, poster_path: str, size: str = DEFAULT_SIZE) -> Path | None:
        """Return a local file for this poster, downloading it once if needed."""
        if not self.valid(poster_path):
            return None
        if size not in ALLOWED_SIZES:
            size = DEFAULT_SIZE

        target = self.path_for(poster_path, size)
        if target.exists() and target.stat().st_size > 0:
            return target

        lock = self._locks.setdefault(target.name, asyncio.Lock())
        async with lock:
            if target.exists() and target.stat().st_size > 0:
                return target
            url = f"{IMAGE_ROOT}/{size}{poster_path}"
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url)
            except httpx.HTTPError:
                return None
            if response.status_code != 200 or not response.content:
                return None
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(response.content)
            tmp.replace(target)
            return target

    def stats(self) -> dict[str, int]:
        files = [f for f in self.dir.glob("*") if f.is_file() and not f.name.endswith(".part")]
        return {
            "count": len(files),
            "bytes": sum(f.stat().st_size for f in files),
        }

    def clear(self) -> int:
        removed = 0
        for f in self.dir.glob("*"):
            if f.is_file():
                f.unlink(missing_ok=True)
                removed += 1
        return removed
