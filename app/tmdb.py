"""TMDB metadata client.

- API key lives server-side only (env TMDB_API_KEY); never sent to the Mini App.
- Every response is cached in the ``tmdb_cache`` table; failures never raise
  into the indexing pipeline — callers get ``None`` and the title keeps a
  placeholder poster.
- Posters are stored as full image URLs (image.tmdb.org), not downloaded.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import time
from typing import Any

import aiohttp

from .db import DB, _j, _uj
from .parser import ParsedName, normalize_title

log = logging.getLogger("mediabot.tmdb")

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
CACHE_TTL = 30 * 24 * 3600  # 30 days


def _img(path: str | None) -> str:
    return f"{IMG}{path}" if path else ""


class TMDB:
    def __init__(self, db: DB, api_key: str, session: aiohttp.ClientSession | None = None):
        self.db = db
        self.api_key = api_key
        self._session = session
        self._lock = asyncio.Lock()

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -- cache -----------------------------------------------------------
    def _cache_get(self, key: str) -> Any | None:
        row = self.db.query_one("SELECT payload, fetched_at FROM tmdb_cache WHERE key=?", (key,))
        if not row:
            return None
        if time.time() - row["fetched_at"] > CACHE_TTL:
            return None
        return _uj(row["payload"])

    def _cache_set(self, key: str, payload: Any) -> None:
        self.db.execute(
            "INSERT INTO tmdb_cache(key, payload, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (key, _j(payload), time.time()),
        )

    async def _get(self, path: str, params: dict) -> dict | None:
        if not self.api_key:
            return None
        key = f"GET:{path}:{_j(sorted(params.items()))}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            sess = await self._sess()
            async with self._lock:  # gentle on TMDB rate limits
                async with sess.get(
                    f"{API}{path}", params={"api_key": self.api_key, **params}
                ) as resp:
                    if resp.status != 200:
                        log.warning("tmdb %s -> %s", path, resp.status)
                        return None
                    data = await resp.json()
            self._cache_set(key, data)
            return data
        except Exception as e:  # never break indexing on TMDB trouble
            log.warning("tmdb %s failed: %r", path, e)
            return None

    # -- matching ----------------------------------------------------------
    @staticmethod
    def _score(candidate_title: str, want: str, want_year: int | None,
               cand_year: int | None, popularity: float) -> float:
        s = difflib.SequenceMatcher(None, normalize_title(candidate_title), want).ratio()
        if want_year and cand_year:
            if want_year == cand_year:
                s += 0.35
            elif abs(want_year - cand_year) == 1:
                s += 0.1
            else:
                s -= 0.4
        s += min(popularity / 200.0, 0.15)  # popular titles win ties
        return s

    @staticmethod
    def _year_of(item: dict, key: str) -> int | None:
        d = item.get(key) or ""
        return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None

    async def match(self, p: ParsedName) -> dict | None:
        """Return a normalized metadata dict or None when no confident match."""
        kind = "movie" if p.kind == "movie" else "tv"
        data = await self._get(
            f"/search/{kind}", {"query": p.title, **({"year": p.year} if p.year else {})}
        )
        if not data or not data.get("results"):
            return None
        want = normalize_title(p.title)
        best, best_s = None, 0.0
        for item in data["results"][:10]:
            cand = item.get("title") or item.get("name") or ""
            cy = self._year_of(item, "release_date" if kind == "movie" else "first_air_date")
            s = self._score(cand, want, p.year, cy, item.get("popularity") or 0)
            if s > best_s:
                best, best_s = item, s
        if best is None or best_s < 0.75:
            log.info("tmdb low-confidence for %r (%.2f) — leaving unmatched", p.raw, best_s)
            return None
        detail = await self._get(f"/{kind}/{best['id']}", {"append_to_response": "credits"})
        d = detail or best
        year = self._year_of(d, "release_date" if kind == "movie" else "first_air_date")
        return {
            "tmdb_id": d.get("id"),
            "kind": "movie" if kind == "movie" else "series",
            "title": d.get("title") or d.get("name") or p.title,
            "orig_title": d.get("original_title") or d.get("original_name") or "",
            "alt_titles": [],
            "year": year or p.year,
            "poster": _img(d.get("poster_path")),
            "backdrop": _img(d.get("backdrop_path")),
            "overview": d.get("overview") or "",
            "genres": [g["name"] for g in d.get("genres", [])],
            "rating": d.get("vote_average"),
            "popularity": d.get("popularity"),
            "lang": d.get("original_language") or "",
        }

    async def season_poster(self, tmdb_id: int, season_no: int) -> str:
        data = await self._get(f"/tv/{tmdb_id}/season/{season_no}", {})
        if not data:
            return ""
        return _img(data.get("poster_path"))

    async def episode_title(self, tmdb_id: int, season_no: int, ep_no: int) -> str:
        data = await self._get(f"/tv/{tmdb_id}/season/{season_no}/episode/{ep_no}", {})
        if not data:
            return ""
        return data.get("name") or ""
