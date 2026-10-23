"""MN IMDb poster fallback — last resort when TMDB has no match.

Free third-party API, no key: ``GET {base}/api/search?q=<title>`` returns
OMDb-shaped results (Title / Year / imdbID / Type / Poster). It is used
ONLY to fill ``titles.poster`` for titles TMDB left unmatched: it never
sets tmdb_id and never merges titles, so a bad hit can cost a poster,
never catalog structure. Matching is therefore deliberately strict —
exact normalized title, kind-compatible Type, year within ±1 — because
a wrong poster is worse than a missing one.

Base URL via env MN_IMDB_BASE_URL (default https://mn-api-imdb.vercel.app);
set it empty to disable. Unknown free-tier limits, so calls are paced
(~1s apart) and a circuit breaker (mirroring the LLM fallback's) stops
all attempts for ~10 minutes after a 429 or repeated 5xx/timeouts.
Failures never raise into the indexing pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

from .parser import normalize_title

log = logging.getLogger("mediabot.mnimdb")

DEFAULT_BASE = "https://mn-api-imdb.vercel.app"
MIN_INTERVAL = 1.0        # pace: at most ~1 request per second
BREAKER_SECONDS = 600      # cooldown once the breaker trips
FAIL_THRESHOLD = 3         # consecutive 5xx/timeouts that trip it

SERIES_TYPES = {"tv series", "tv mini-series"}
MOVIE_TYPES = {"feature", "movie", "tv movie"}


class MNImdb:
    def __init__(self, base_url: str | None = None,
                 session: aiohttp.ClientSession | None = None):
        if base_url is None:
            base_url = os.environ.get("MN_IMDB_BASE_URL", DEFAULT_BASE)
        self.base_url = (base_url or "").rstrip("/")
        self._session = session
        self._lock = asyncio.Lock()
        self._next_call_at = 0.0
        self._fail_streak = 0
        # circuit breaker, same shape as LLTitle.rate_limited_until
        self.unavailable_until = 0.0

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -- circuit breaker ---------------------------------------------------
    def _trip_breaker(self, why: str) -> None:
        self.unavailable_until = time.time() + BREAKER_SECONDS
        log.warning("mn imdb %s — pausing fallback for %ds",
                    why, BREAKER_SECONDS)

    def _note_failure(self, why: str) -> None:
        self._fail_streak += 1
        if self._fail_streak >= FAIL_THRESHOLD:
            self._fail_streak = 0
            self._trip_breaker(f"failing repeatedly ({why})")

    # -- API -----------------------------------------------------------------
    async def search(self, title: str) -> list[dict] | None:
        """Raw candidate list for a title query.

        None means the request itself failed (breaker bookkeeping done);
        an empty list means the API answered with no results."""
        if not self.available or not title:
            return None
        if time.time() < self.unavailable_until:
            return None  # breaker open: fail fast, no HTTP
        async with self._lock:  # serialize: pacing is per-client
            wait = self._next_call_at - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_call_at = time.time() + MIN_INTERVAL
            try:
                sess = await self._sess()
                async with sess.get(f"{self.base_url}/api/search",
                                    params={"q": title}) as resp:
                    if resp.status == 429:
                        self._trip_breaker("rate-limited (429)")
                        return None
                    if resp.status != 200:
                        log.warning("mn imdb search -> %s", resp.status)
                        self._note_failure(f"http {resp.status}")
                        return None
                    data = await resp.json()
            except Exception as e:  # never break enrichment on API trouble
                log.warning("mn imdb search failed: %r", e)
                self._note_failure(repr(e))
                return None
        self._fail_streak = 0
        results = data.get("Search") if isinstance(data, dict) else None
        return results if isinstance(results, list) else []

    async def poster_for(self, title: str, year: int | None,
                         kind: str) -> str | None:
        """Poster URL for a confident match, else None."""
        results = await self.search(title)
        if not results:
            return None
        return self.pick(results, normalize_title(title), year, kind)

    # -- matching --------------------------------------------------------------
    @staticmethod
    def _year_of(item: dict) -> int | None:
        y = item.get("Year") or ""
        return int(y[:4]) if len(y) >= 4 and y[:4].isdigit() else None

    @staticmethod
    def pick(candidates: list[dict], want_norm: str, want_year: int | None,
             want_kind: str) -> str | None:
        """Choose a poster from search candidates, conservatively.

        A candidate survives only if it has a real poster (not "N/A"),
        a tt-prefixed IMDb id (person entries are nm-prefixed), a Type
        compatible with the catalog kind (junk like "musicVideo" is in
        neither set), and a normalized title exactly equal to ours.
        Among survivors the closest year wins; a known catalog year
        tolerates ±1, anything further is rejected outright."""
        kinds = SERIES_TYPES if want_kind == "series" else MOVIE_TYPES
        best_poster, best_dist = None, None
        for item in candidates:
            poster = item.get("Poster") or ""
            if not poster.startswith("http"):
                continue  # covers "N/A" and missing
            if not (item.get("imdbID") or "").startswith("tt"):
                continue
            if (item.get("Type") or "").strip().lower() not in kinds:
                continue
            if normalize_title(item.get("Title") or "") != want_norm:
                continue
            cand_year = MNImdb._year_of(item)
            if want_year and cand_year:
                dist = abs(want_year - cand_year)
                if dist > 1:
                    continue
            elif want_year or cand_year:
                dist = 1  # one side unknown: acceptable, but loses ties
            else:
                dist = 0
            if best_dist is None or dist < best_dist:
                best_poster, best_dist = poster, dist
        return best_poster
