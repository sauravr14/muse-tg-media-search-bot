"""FastAPI backend: public catalog API, Mini App hosting, admin API.

Security notes:
- TMDB key and bot token never leave the server; the SPA only gets poster URLs.
- Every /api/admin/* route requires a Telegram WebApp initData payload from a
  user id listed in ADMIN_USER_IDS (HMAC-verified server-side).
- Public search is rate-limited per IP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .db import DB, RES_ORDER, _uj

log = logging.getLogger("mediabot.api")
APP_DIR = Path(__file__).parent / "webapp"

# -- webapp initData validation --------------------------------------------------
def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Verify Telegram WebApp initData. Returns the parsed dict or None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", "")
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        user = json.loads(pairs.get("user", "{}"))
        return {"user_id": user.get("id"), "raw": pairs}
    except Exception:
        return None


# -- rate limiting -----------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_min: int = 120):
        self.per_min = per_min
        self._hits: dict[str, list[float]] = {}
        self._prune_at = 0.0

    def check(self, ip: str) -> bool:
        now = time.time()
        # periodically drop IPs whose hits all expired (unbounded dict = memory leak)
        if now >= self._prune_at:
            self._prune_at = now + 300
            self._hits = {k: [t for t in v if now - t < 60]
                          for k, v in self._hits.items()}
            self._hits = {k: v for k, v in self._hits.items() if v}
        hits = [t for t in self._hits.get(ip, []) if now - t < 60]
        if len(hits) >= self.per_min:
            return False
        hits.append(now)
        self._hits[ip] = hits
        return True


def _file_dict(f) -> dict:
    return {"id": f["id"], "name": f["file_name"], "resolution": f["resolution"],
            "quality": f["quality"], "codec": f["codec"], "size": f["size"],
            "duration": f["duration"], "pack_label": f["pack_label"] or ""}


# Public lists show only TMDB-matched titles that already have a poster. That is
# what keeps Home instant (no placeholder tiles) and what hides the
# "Movie Name We / BI / X26" junk variants until the enricher merges them.
# The WHERE text matches the partial indexes in db._migrate_v2 exactly.
MATCHED = "tmdb_id IS NOT NULL AND poster != ''"
CLEAN = " AND has_clean=1"  # denormalised "has a non-theatre-print file"


class TTLCache:
    """Tiny in-process cache; the catalog changes slowly, 30-120 s is invisible."""

    def __init__(self, maxsize: int = 1024):
        self.maxsize = maxsize
        self._d: dict = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        return None

    def set(self, key, value, ttl: float):
        if len(self._d) >= self.maxsize:
            now = time.time()
            self._d = {k: v for k, v in self._d.items() if v[0] > now}
            if len(self._d) >= self.maxsize:
                self._d.clear()
        self._d[key] = (time.time() + ttl, value)
        return value


def _thumb(url: str, size: str = "w342") -> str:
    """Grid posters at w342 (about half the bytes of the stored w500)."""
    return url.replace("/t/p/w500/", f"/t/p/{size}/") if url else ""


def _title_card(t, big: bool = False) -> dict:
    return {"id": t["id"], "kind": t["kind"], "title": t["title"],
            "year": t["year"], "poster": t["poster"] if big else _thumb(t["poster"]),
            "rating": t["rating"],
            "genres": _uj(t["genres"], []), "overview": (t["overview"] or "")[:220]}


def _hero_card(t) -> dict:
    d = _title_card(t)
    d["backdrop"] = _thumb(t["backdrop"], "w780")
    d["overview"] = (t["overview"] or "")[:280]
    return d


def _fts_match(q: str) -> str:
    """Safe FTS5 prefix query: word tokens only (':' '-' '"' break raw FTS)."""
    terms = re.findall(r"\w+", q.lower())[:8]
    return " ".join(f'"{t}"*' for t in terms)


def _client_ip(request: Request) -> str:
    # Behind the Vercel rewrite every request comes from Vercel's IPs; without
    # this, the per-IP limiter put ALL users in one 60/min bucket.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def create_app(cfg: Config, db: DB, bot_username: str = "", bot=None) -> FastAPI:
    app = FastAPI(title="Media Library API")
    limiter = RateLimiter()
    send_limiter = RateLimiter(per_min=20)
    cache = TTLCache()

    origins = [o.strip() for o in (cfg.cors_origins or "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins or ["*"],
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=800)

    @app.on_event("startup")
    async def _limit_threads():
        # Sync endpoints run in a threadpool (so SQLite never blocks the event
        # loop the bot shares). Keep it small: each thread owns a connection.
        try:
            import anyio.to_thread
            anyio.to_thread.current_default_thread_limiter().total_tokens = 8
        except Exception:
            pass

    @app.middleware("http")
    async def _cache_headers(request: Request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if request.method == "GET" and resp.status_code == 200:
            if p in ("/api/home", "/api/recent", "/api/browse"):
                resp.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=300"
            elif p == "/api/search" or p.startswith("/api/title/"):
                resp.headers["Cache-Control"] = "public, max-age=20, stale-while-revalidate=120"
        return resp

    def _admin(request: Request, init_data: str = "") -> int:
        init_data = init_data or request.headers.get("x-telegram-init-data", "")
        data = validate_init_data(init_data, cfg.bot_token) if init_data else None
        if not data or not cfg.is_admin(data.get("user_id")):
            raise HTTPException(403, "admin only")
        return int(data["user_id"])

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # NOTE: the catalog endpoints below are plain `def` on purpose. FastAPI runs
    # them in a threadpool, so a slow SQLite query can never freeze the asyncio
    # loop that also drives the Telegram connection (that starvation made the
    # Mini App show blank skeletons and Pyrogram time out while indexing).

    # -- public catalog -----------------------------------------------------------
    def _recent_rows(kind: str, limit: int, hide_cam: int, extra: str = ""):
        k = kind in ("movie", "series")
        return db.query(
            f"SELECT * FROM titles WHERE {MATCHED}" + (" AND kind=?" if k else "")
            + (CLEAN if hide_cam else "") + extra
            + " ORDER BY last_file_at DESC LIMIT ?",
            tuple(([kind] if k else []) + [limit]))

    @app.get("/api/home")
    def home(hide_cam: int = 0):
        """Everything Home needs in ONE request, cached for 45 s."""
        ck = ("home", hide_cam)
        hit = cache.get(ck)
        if hit is not None:
            return hit
        clean = CLEAN if hide_cam else ""
        heroes = []
        for kind in ("movie", "series"):
            heroes += db.query(
                f"SELECT * FROM titles WHERE {MATCHED} AND kind=? AND backdrop != ''"
                f" AND overview != ''{clean} ORDER BY last_file_at DESC LIMIT 4", (kind,))
        heroes.sort(key=lambda t: t["last_file_at"], reverse=True)
        trending = db.query(
            f"SELECT * FROM titles WHERE {MATCHED}{clean} "
            "ORDER BY popularity DESC LIMIT 20")
        top = []
        for kind in ("movie", "series"):
            top += db.query(
                f"SELECT * FROM titles WHERE {MATCHED} AND kind=? AND popularity > 15"
                f"{clean} ORDER BY rating DESC LIMIT 12", (kind,))
        top.sort(key=lambda t: t["rating"] or 0, reverse=True)
        out = {"hero": [_hero_card(t) for t in heroes[:6]],
               "rows": [
                   {"key": "movies", "title": "Recently added movies",
                    "results": [_title_card(t) for t in _recent_rows("movie", 20, hide_cam)]},
                   {"key": "series", "title": "Recently added series",
                    "results": [_title_card(t) for t in _recent_rows("series", 20, hide_cam)]},
                   {"key": "trending", "title": "Trending now",
                    "results": [_title_card(t) for t in trending]},
                   {"key": "top", "title": "Top rated",
                    "results": [_title_card(t) for t in top[:20]]},
               ]}
        out["rows"] = [r for r in out["rows"] if r["results"]]
        return cache.set(ck, out, 45)

    @app.get("/api/search")
    def search(request: Request, q: str = Query("", max_length=120),
               kind: str = "", page: int = 1, hide_cam: int = 0):
        if not limiter.check(_client_ip(request)):
            raise HTTPException(429, "slow down")
        q = q.strip()
        match = _fts_match(q)
        page = max(page, 1)
        if not match:
            return {"results": [], "page": page}
        ck = ("search", match, kind, page, hide_cam)
        hit = cache.get(ck)
        if hit is not None:
            return hit
        k = kind in ("movie", "series")
        per, off = 20, (page - 1) * 20
        nq = " ".join(re.findall(r"\w+", q.lower()))
        base = ("SELECT t.* FROM titles t WHERE t.id IN ("
                "SELECT rowid FROM titles_fts WHERE titles_fts MATCH ? ORDER BY rank LIMIT 400)"
                + (" AND t.kind=?" if k else "") + (CLEAN.replace("has_clean", "t.has_clean")
                                                    if hide_cam else ""))
        extra = [kind] if k else []
        try:
            rows = db.query(
                base + " AND t.tmdb_id IS NOT NULL"
                " ORDER BY (t.norm_title=?) DESC, (t.norm_title LIKE ?) DESC,"
                " t.popularity DESC, t.last_file_at DESC LIMIT ? OFFSET ?",
                tuple([match] + extra + [nq, nq + "%", per, off]))
            results = [_title_card(t) for t in rows]
            # Obscure/regional titles TMDB can't match stay findable, but only
            # when the matched results are thin, collapsed to one card per
            # name, and never as a "<matched title> + junk" duplicate.
            if page == 1 and len(results) < 5:
                un = db.query(
                    base + " AND t.tmdb_id IS NULL ORDER BY t.last_file_at DESC LIMIT 40",
                    tuple([match] + extra))
                matched_norms = [t["norm_title"] for t in rows]
                seen: list[str] = []
                for t in sorted(un, key=lambda r: len(r["norm_title"])):
                    n = t["norm_title"]
                    if any(n == m or n.startswith(m + " ") for m in matched_norms):
                        continue
                    if any(n.startswith(sn + " ") or n == sn for sn in seen):
                        continue
                    seen.append(n)
                    results.append(_title_card(t))
                    if len(results) >= 10:
                        break
        except Exception as e:
            log.warning("search failed for %r: %r", q, e)
            results = []
        return cache.set(ck, {"results": results, "page": page}, 30)

    @app.get("/api/recent")
    def recent(kind: str = "", limit: int = 24, hide_cam: int = 0):
        limit = min(max(limit, 1), 60)
        ck = ("recent", kind, limit, hide_cam)
        hit = cache.get(ck)
        if hit is not None:
            return hit
        rows = _recent_rows(kind, limit, hide_cam)
        return cache.set(ck, {"results": [_title_card(t) for t in rows]}, 45)

    @app.post("/api/getfile")
    async def getfile(request: Request):
        """Mini App Get button: bot delivers the file straight to the user's DM.

        Needs a valid Telegram WebApp initData (proves the Telegram user id).
        No deep-link round-trip, so it can't silently fail like ?start= links.
        """
        if bot is None:
            raise HTTPException(503, "bot not ready")
        init_data = request.headers.get("x-telegram-init-data", "")
        data = validate_init_data(init_data, cfg.bot_token) if init_data else None
        user_id = (data or {}).get("user_id")
        if not user_id:
            raise HTTPException(401, "open this app from Telegram")
        if not send_limiter.check(f"u{user_id}"):
            raise HTTPException(429, "too many requests, slow down")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad request")
        ok, msg = await bot.deliver_to_user(int(user_id), body.get("file_id"))
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True}

    @app.get("/api/browse")
    def browse(kind: str = "", sort: str = "recent",
               page: int = 1, per: int = 30, hide_cam: int = 0):
        """Paginated catalog for the Movies/Series tabs (matched titles only)."""
        # "id" tie-breakers match the partial indexes, so each sort is an index scan
        sorts = {
            "recent": "last_file_at DESC, id ASC",
            "az": "title COLLATE NOCASE ASC, id ASC",
            "rating": "rating DESC, id ASC",
            "year": "year DESC, id ASC",
        }
        order = sorts.get(sort, sorts["recent"])
        per = min(max(per, 1), 60)
        page = max(page, 1)
        ck = ("browse", kind, sort, page, per, hide_cam)
        hit = cache.get(ck)
        if hit is not None:
            return hit
        k = kind in ("movie", "series")
        where = f"WHERE {MATCHED}" + (" AND kind=?" if k else "") + (CLEAN if hide_cam else "")
        params: list = [kind] if k else []
        cck = ("count", kind, hide_cam)
        total = cache.get(cck)
        if total is None:
            total = cache.set(cck, db.query_one(
                f"SELECT COUNT(*) c FROM titles {where}", tuple(params))["c"], 120)
        rows = db.query(
            f"SELECT * FROM titles {where} ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params + [per, (page - 1) * per]))
        return cache.set(ck, {"results": [_title_card(t) for t in rows], "page": page,
                              "per": per, "total": total, "has_more": page * per < total}, 45)

    @app.get("/api/title/{title_id}")
    def title_detail(title_id: int, hide_cam: int = 0):
        t = db.query_one("SELECT * FROM titles WHERE id=?", (title_id,))
        if not t:
            raise HTTPException(404, "not found")
        out = _title_card(t, big=True)
        out.update({"orig_title": t["orig_title"], "backdrop": _thumb(t["backdrop"], "w780"),
                    "overview": t["overview"], "popularity": t["popularity"],
                    "lang": t["lang"], "added_at": t["added_at"]})
        if t["kind"] == "movie":
            files = db.query(
                "SELECT * FROM files WHERE title_id=? AND episode_id IS NULL "
                "AND season_id IS NULL AND status='ok'"
                + (" AND is_cam=0" if hide_cam else "")
                + " ORDER BY " + RES_ORDER + " DESC", (title_id,))
            out["versions"] = [_file_dict(f) for f in files]
        else:
            cam = " AND is_cam=0" if hide_cam else ""
            # ONE query for every file of this title, grouped in Python.
            # (was: 2 queries per episode -> 18s for Game of Thrones)
            by_ep: dict[int, list] = {}
            packs_by_season: dict[int, list] = {}
            for f in db.query(
                    "SELECT * FROM files WHERE title_id=? AND status='ok'" + cam
                    + " ORDER BY " + RES_ORDER + " DESC", (title_id,)):
                if f["episode_id"]:
                    by_ep.setdefault(f["episode_id"], []).append(f)
                elif f["season_id"]:
                    packs_by_season.setdefault(f["season_id"], []).append(f)
            seasons = []
            for se in db.query("SELECT * FROM seasons WHERE title_id=? ORDER BY season_no",
                               (title_id,)):
                eps = []
                for e in db.query("SELECT * FROM episodes WHERE season_id=? ORDER BY ep_no",
                                  (se["id"],)):
                    vers = by_ep.get(e["id"], [])
                    eps.append({"ep_no": e["ep_no"], "title": e["ep_title"],
                                "air_date": e["air_date"], "overview": e["overview"],
                                "versions": [_file_dict(f) for f in vers]})
                seasons.append({"season_no": se["season_no"], "poster": se["poster"],
                                "overview": se["overview"], "episodes": eps,
                                "packs": [_file_dict(f)
                                          for f in packs_by_season.get(se["id"], [])]})
            out["seasons"] = seasons
        return out

    @app.get("/api/deeplink/file/{file_id}")
    def deeplink(file_id: int):
        f = db.query_one("SELECT id FROM files WHERE id=?", (file_id,))
        if not f or not bot_username:
            raise HTTPException(404, "not found")
        return {"url": f"https://t.me/{bot_username}?start=file_{file_id}"}

    @app.get("/api/stats")
    def stats():
        return db.stats()

    # -- admin ----------------------------------------------------------------------
    @app.get("/api/admin/overview")
    def admin_overview(request: Request):
        _admin(request)
        return {"stats": db.stats(),
                "sources": [dict(s) for s in db.list_sources()]}

    @app.get("/api/admin/unmatched")
    def admin_unmatched(request: Request, limit: int = 50):
        _admin(request)
        rows = db.query(
            "SELECT * FROM files WHERE title_id IS NULL ORDER BY indexed_at DESC LIMIT ?",
            (min(limit, 200),))
        return {"files": [_file_dict(f) | {"file_name": f["file_name"],
                                           "tg": [f["tg_chat_id"], f["tg_msg_id"]]} for f in rows]}

    @app.post("/api/admin/file/{file_id}/hide")
    def admin_hide(request: Request, file_id: int):
        _admin(request)
        db.execute("UPDATE files SET status='hidden' WHERE id=?", (file_id,))
        return {"ok": True}

    @app.post("/api/admin/title/{title_id}/retitle")
    async def admin_retitle(request: Request, title_id: int):
        _admin(request)
        body = await request.json()
        title = (body.get("title") or "").strip()
        year = body.get("year")
        if not title:
            raise HTTPException(400, "title required")
        from .parser import normalize_title
        db.execute("UPDATE titles SET title=?, norm_title=?, year=?, updated_at=? WHERE id=?",
                   (title, normalize_title(title), year, time.time(), title_id))
        return {"ok": True}

    # -- mini app static --------------------------------------------------------------
    if APP_DIR.exists():
        app.mount("/app", StaticFiles(directory=str(APP_DIR), html=True), name="app")

    @app.get("/")
    async def root():
        return {"service": "media-library", "app": "/app/", "health": "/healthz"}

    return app
