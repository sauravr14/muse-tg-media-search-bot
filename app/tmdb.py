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


class TMDBError(Exception):
    """Transient TMDB failure (network reset, 429, 5xx).

    Distinct from "TMDB has no such title": callers must retry soon and must
    NOT park the title for days (the old code did, so a single reset
    connection left a title unmatched for a week)."""


class TMDB:
    def __init__(self, db: DB, api_key: str, session: aiohttp.ClientSession | None = None):
        self.db = db
        self.api_key = api_key
        self._session = session
        # a few requests in flight instead of one global lock; TMDB allows
        # far more, this keeps us polite while matching ~4x faster
        self._sem = asyncio.Semaphore(4)

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
            async with self._sem:
                async with sess.get(
                    f"{API}{path}", params={"api_key": self.api_key, **params}
                ) as resp:
                    if resp.status == 404:
                        return None  # definitive: no such record
                    if resp.status != 200:
                        log.warning("tmdb %s -> %s", path, resp.status)
                        raise TMDBError(f"HTTP {resp.status}")
                    data = await resp.json()
        except TMDBError:
            raise
        except Exception as e:  # reset / timeout / DNS: transient
            log.warning("tmdb %s failed: %r", path, e)
            raise TMDBError(repr(e)) from e
        self._cache_set(key, data)
        return data

    # -- matching ----------------------------------------------------------
    MIN_SIM = 0.62  # title similarity floor, BEFORE any year/popularity bonus

    @classmethod
    def _score(cls, candidate_title: str, want: str, want_year: int | None,
               cand_year: int | None, popularity: float,
               alt_title: str = "") -> float:
        s = difflib.SequenceMatcher(None, normalize_title(candidate_title), want).ratio()
        if alt_title:
            s = max(s, difflib.SequenceMatcher(None, normalize_title(alt_title), want).ratio())
        if s < cls.MIN_SIM:
            # a matching year used to lift 0.45-similarity junk over the bar
            # and attach unrelated files to the wrong movie
            return 0.0
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
        # try the full title, then progressively drop trailing junk words
        # ("The Crown Willsmania" -> "The Crown")
        words = p.title.split()
        queries = [" ".join(words)]
        for drop in (1, 2):
            if len(words) - drop >= 2:
                queries.append(" ".join(words[:-drop]))
        for q in dict.fromkeys(queries):
            meta = await self._match_query(q, p)
            if meta:
                return meta
        return None

    async def _match_query(self, query: str, p: ParsedName) -> dict | None:
        kind = "movie" if p.kind == "movie" else "tv"
        data = await self._get(
            f"/search/{kind}", {"query": query, **({"year": p.year} if p.year else {})}
        )
        if p.year and (not data or not data.get("results")):
            # release year in filenames is often off by one / re-release year
            data = await self._get(f"/search/{kind}", {"query": query})
        if not data or not data.get("results"):
            return None
        want = normalize_title(query)
        best, best_s = None, 0.0
        for item in data["results"][:10]:
            cand = item.get("title") or item.get("name") or ""
            cy = self._year_of(item, "release_date" if kind == "movie" else "first_air_date")
            s = self._score(cand, want, p.year, cy, item.get("popularity") or 0,
                            item.get("original_title") or item.get("original_name") or "")
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
        try:
            data = await self._get(f"/tv/{tmdb_id}/season/{season_no}", {})
        except TMDBError:
            return ""
        if not data:
            return ""
        return _img(data.get("poster_path"))

    async def episode_title(self, tmdb_id: int, season_no: int, ep_no: int) -> str:
        try:
            data = await self._get(f"/tv/{tmdb_id}/season/{season_no}/episode/{ep_no}", {})
        except TMDBError:
            return ""
        if not data:
            return ""
        return data.get("name") or ""
