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
import mimetypes
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .db import DB, RES_ORDER, _uj, fts_terms
from .owner import fsub_list
from .dbthread import run_db
from .streamer import (MAX_STREAMS, ByteStreamer, StreamFileNotFound,
                       parse_range, sign_token, stream_secret, verify_token)

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
    def __init__(self, per_min: int = 60):
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


CLEAN_ONLY = (" AND EXISTS (SELECT 1 FROM files f WHERE f.title_id=titles.id"
                " AND f.is_cam=0 AND f.status='ok')")


def _title_card(t) -> dict:
    return {"id": t["id"], "kind": t["kind"], "title": t["title"],
            "year": t["year"], "poster": t["poster"], "rating": t["rating"],
            "genres": _uj(t["genres"], []), "overview": (t["overview"] or "")[:220]}


def create_app(cfg: Config, db: DB, bot_username: str = "", bot=None) -> FastAPI:
    app = FastAPI(title="Media Library API")
    limiter = RateLimiter()
    streamer = ByteStreamer(bot.client) if bot is not None else None
    secret = stream_secret(cfg.bot_token, cfg.stream_secret)

    def _admin(request: Request, init_data: str = "") -> int:
        init_data = init_data or request.headers.get("x-telegram-init-data", "")
        data = validate_init_data(init_data, cfg.bot_token) if init_data else None
        if not data or not cfg.is_admin(data.get("user_id")):
            raise HTTPException(403, "admin only")
        return int(data["user_id"])

    # -- force subscribe ---------------------------------------------------------
    # Catalog browsing is gated ONLY when fsub is non-empty. When it is
    # empty, behaviour is exactly the old anonymous behaviour (no initData
    # required). When non-empty, the webapp sends initData and we return
    # 403 {"fsub_required": true, "channels": [...]} for unjoined users
    # (or for unverifiable users, so the app can show the join list).
    # Get/stream already require initData, so they gate on the real user id.
    # If bot is None (test app without a client), catalog gating fails open
    # — production always passes bot (main.py), so this only affects tests.
    async def _fsub_gate(request: Request, user_id: int | None):
        entries = await run_db(fsub_list, db)
        if not entries:
            return None
        if user_id is None:
            return entries
        if bot is None:
            return None
        unjoined = await bot.unjoined_fsub(int(user_id))
        return unjoined or None

    def _fsub_response(channels):
        return JSONResponse(status_code=403,
                            content={"fsub_required": True, "channels": channels})

    async def _catalog_gate(request: Request):
        init_data = request.headers.get("x-telegram-init-data", "")
        data = validate_init_data(init_data, cfg.bot_token) if init_data else None
        user_id = (data or {}).get("user_id")
        gated = await _fsub_gate(request, user_id)
        return _fsub_response(gated) if gated else None

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # -- public catalog -----------------------------------------------------------
    @app.get("/api/search")
    async def search(request: Request, q: str = Query("", max_length=120),
                     kind: str = "", page: int = 1, hide_cam: int = 0):
        if not limiter.check(request.client.host if request.client else "?"):
            raise HTTPException(429, "slow down")
        gated = await _catalog_gate(request)
        if gated is not None:
            return gated
        q = q.strip()
        if not q:
            return {"results": [], "page": page}
        # strip stopwords first: a lone common-word prefix query ("the")
        # scans a huge slice of the catalog and takes 15-30s to rank.
        terms = fts_terms(q)
        if not terms:
            return {"results": [], "page": page}
        # each term matches itself OR its singular form: FTS prefix
        # matching is one-directional, so "dragons" alone never matched
        # the title token "dragon" and the real House of the Dragon
        # vanished from its own search. Groups MUST be joined with an
        # explicit AND: adjacent parenthesised groups are an FTS5 syntax
        # error ("black panther" returned no results at all, 2026-10-22)
        # and the except below silently turned that into an empty list.
        groups = []
        for t in terms:
            variants = {t}
            if t.endswith("s") and len(t) > 3:
                variants.add(t[:-1])
            groups.append("(" + " OR ".join(f'"{v}"*' for v in sorted(variants)) + ")")
        match = " AND ".join(groups)
        per, off = 20, (max(page, 1) - 1) * 20
        try:
            rows = await run_db(
                db.query,
                "SELECT t.* FROM titles_fts f JOIN titles t ON t.id=f.rowid "
                "WHERE titles_fts MATCH ?"
                + (" AND t.kind=? " if kind in ("movie", "series") else "")
                + (CLEAN_ONLY.replace("titles.id", "t.id") if hide_cam else "")
                + " ORDER BY t.popularity DESC NULLS LAST, t.last_file_at DESC"
                " LIMIT ? OFFSET ?",
                tuple([match] + ([kind] if kind in ("movie", "series") else []) + [per, off]))
        except Exception:
            rows = []
        return {"results": [_title_card(t) for t in rows], "page": page}

    @app.get("/api/recent")
    async def recent(request: Request, kind: str = "", limit: int = 24, hide_cam: int = 0):
        gated = await _catalog_gate(request)
        if gated is not None:
            return gated
        limit = min(max(limit, 1), 60)
        # tmdb_id IS NOT NULL is load-bearing: it lets SQLite use the
        # idx_titles_home partial index. The old ORDER BY (poster != '')
        # forced a full-catalog temp sort (~60s); poster-first ordering now
        # comes from the index's WHERE clause instead.
        where = " WHERE tmdb_id IS NOT NULL AND poster != '' "
        if kind in ("movie", "series"):
            where += "AND kind=? "
        rows = await run_db(
            db.query,
            "SELECT * FROM titles" + where
            + (CLEAN_ONLY if hide_cam else "")
            + " ORDER BY last_file_at DESC LIMIT ?",
            tuple(([kind] if kind in ("movie", "series") else []) + [limit]))
        return {"results": [_title_card(t) for t in rows]}

    @app.get("/api/home")
    async def home(request: Request, hide_cam: int = 0):
        gated = await _catalog_gate(request)
        if gated is not None:
            return gated
        """Everything the Home tab needs in one request: a hero carousel
        (TMDB-matched titles with backdrops) plus poster-first rails.
        Ported from the 2026-10-22 optimized build's /api/home onto this
        API's helpers, so the Netflix-style webapp works on this backend."""
        clean = CLEAN_ONLY if hide_cam else ""

        def hero_card(t) -> dict:
            c = _title_card(t)
            c["backdrop"] = t["backdrop"]
            return c

        heroes: list = []
        for kind in ("movie", "series"):
            heroes += await run_db(
                db.query,
                # tmdb_id IS NOT NULL is load-bearing: it lets SQLite use
                # the idx_titles_home partial index. Without it the planner
                # sorts every title of that kind in a temp b-tree (~40s).
                "SELECT * FROM titles WHERE kind=? AND tmdb_id IS NOT NULL"
                " AND backdrop != '' AND overview != '' AND poster != ''"
                + clean
                + " ORDER BY last_file_at DESC LIMIT 4", (kind,))
        heroes.sort(key=lambda t: t["last_file_at"], reverse=True)

        async def rail(kind: str, label: str) -> dict:
            rows = await run_db(
                db.query,
                # 2026 releases with posters (2026-10-29): "recently added"
                # by file date went stale when the channel scans finished,
                # so the rails now show the current year's releases instead.
                # tmdb_id IS NOT NULL keeps idx_titles_year usable.
                "SELECT * FROM titles WHERE kind=? AND tmdb_id IS NOT NULL"
                " AND poster != '' AND year=2026" + clean
                + " ORDER BY popularity DESC NULLS LAST LIMIT 18", (kind,))
            return {"title": label, "results": [_title_card(t) for t in rows]}

        trending = await run_db(
            db.query,
            "SELECT * FROM titles WHERE tmdb_id IS NOT NULL AND poster != ''"
            + clean
            + " ORDER BY popularity DESC NULLS LAST LIMIT 18")
        rows = [
            await rail("movie", "2026 Movies"),
            await rail("series", "2026 Series"),
            {"title": "Trending now",
             "results": [_title_card(t) for t in trending]},
        ]
        return {"hero": [hero_card(t) for t in heroes[:6]],
                "rows": [r for r in rows if r["results"]]}

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
        gated = await _fsub_gate(request, int(user_id))
        if gated:
            return _fsub_response(gated)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad request")
        ok, msg = await bot.deliver_to_user(int(user_id), body.get("file_id"))
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True}

    @app.post("/api/stream_token")
    async def stream_token(request: Request):
        """Mini App Play button: mint a short-lived signed stream URL.

        Needs a valid Telegram WebApp initData (proves the Telegram user
        id). No START needed: unlike Get, streaming delivers nothing to
        the user's DM, so the bot chat state is irrelevant.
        """
        if bot is None or streamer is None:
            raise HTTPException(503, "bot not ready")
        init_data = request.headers.get("x-telegram-init-data", "")
        data = validate_init_data(init_data, cfg.bot_token) if init_data else None
        if not data or not data.get("user_id"):
            raise HTTPException(401, "open this app from Telegram")
        gated = await _fsub_gate(request, int(data["user_id"]))
        if gated:
            return _fsub_response(gated)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad request")
        f = await run_db(db.query_one,
                         "SELECT id, file_name FROM files WHERE id=? AND status='ok'",
                         (body.get("file_id"),))
        if not f:
            raise HTTPException(404, "not found")
        return {"url": "/api/stream/" + sign_token(secret, int(f["id"])),
                "name": f["file_name"]}

    @app.api_route("/api/stream/{token}", methods=["GET", "HEAD"])
    async def stream(request: Request, token: str):
        """Stream one catalog file's bytes with HTTP Range support.

        The token is HMAC-signed and expires after an hour; the browser
        sends ordinary Range requests and the bytes are relayed from
        Telegram's DCs in 1MB chunks (never written to disk).
        """
        if bot is None or streamer is None:
            raise HTTPException(503, "bot not ready")
        file_id = verify_token(secret, token)
        if not file_id:
            raise HTTPException(403, "bad or expired link")
        f = await run_db(db.query_one,
                         "SELECT * FROM files WHERE id=? AND status='ok'",
                         (file_id,))
        if not f:
            raise HTTPException(404, "not found")
        if streamer.active >= MAX_STREAMS:
            raise HTTPException(503, "too many streams, try again shortly")
        try:
            props = await streamer.resolve(f["tg_chat_id"], f["tg_msg_id"])
        except StreamFileNotFound:
            raise HTTPException(404, "not found")
        size = props["file_size"] or f["size"]
        if size <= 0:
            raise HTTPException(404, "not found")
        range_header = request.headers.get("Range", "")
        try:
            start, end = (parse_range(range_header, size) if range_header
                          else (0, size - 1))
        except ValueError:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
        mime = (props["mime_type"]
                or mimetypes.guess_type(props["file_name"])[0]
                or "application/octet-stream")
        safe_name = (props["file_name"] or "file").replace('"', "'")
        safe_name = "".join(c for c in safe_name if c not in "\r\n")
        headers = {
            "Content-Type": mime,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{safe_name}"',
        }
        if request.method == "HEAD":
            return Response(status_code=206 if range_header else 200,
                            headers=headers)

        async def _gen():
            streamer.active += 1
            try:
                async for chunk in streamer.yield_range(props, start, end):
                    yield chunk
            finally:
                streamer.active -= 1

        return StreamingResponse(_gen(),
                                 status_code=206 if range_header else 200,
                                 headers=headers)

    @app.get("/api/browse")
    async def browse(request: Request, kind: str = "", sort: str = "recent",
                     page: int = 1, per: int = 30, hide_cam: int = 0):
        """Paginated full catalog for the Movies/Series tabs."""
        gated = await _catalog_gate(request)
        if gated is not None:
            return gated
        sorts = {
            "recent": "last_file_at DESC",
            "az": "title COLLATE NOCASE ASC",
            "rating": "rating DESC NULLS LAST, popularity DESC NULLS LAST",
            "year": "year DESC NULLS LAST, title COLLATE NOCASE ASC",
        }
        order = sorts.get(sort, sorts["recent"])
        per = min(max(per, 1), 60)
        page = max(page, 1)
        where = "WHERE kind=? " if kind in ("movie", "series") else "WHERE 1=1 "
        params: list = [kind] if kind in ("movie", "series") else []
        if hide_cam:
            where += CLEAN_ONLY
        total = (await run_db(db.query_one,
                              f"SELECT COUNT(*) c FROM titles {where}",
                              tuple(params)))["c"]
        rows = await run_db(
            db.query,
            f"SELECT * FROM titles {where}ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params + [per, (page - 1) * per]))
        return {"results": [_title_card(t) for t in rows], "page": page,
                "per": per, "total": total, "has_more": page * per < total}

    @app.get("/api/title/{title_id}")
    async def title_detail(request: Request, title_id: int, hide_cam: int = 0):
        gated = await _catalog_gate(request)
        if gated is not None:
            return gated
        t = await run_db(db.query_one, "SELECT * FROM titles WHERE id=?",
                         (title_id,))
        if not t:
            raise HTTPException(404, "not found")
        out = _title_card(t)
        out.update({"orig_title": t["orig_title"], "backdrop": t["backdrop"],
                    "overview": t["overview"], "popularity": t["popularity"],
                    "lang": t["lang"], "added_at": t["added_at"]})
        if t["kind"] == "movie":
            files = await run_db(
                db.query,
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
            for f in await run_db(
                    db.query,
                    "SELECT * FROM files WHERE title_id=? AND status='ok'" + cam
                    + " ORDER BY " + RES_ORDER + " DESC", (title_id,)):
                if f["episode_id"]:
                    by_ep.setdefault(f["episode_id"], []).append(f)
                elif f["season_id"]:
                    packs_by_season.setdefault(f["season_id"], []).append(f)
            season_rows = await run_db(
                db.query, "SELECT * FROM seasons WHERE title_id=? ORDER BY season_no",
                (title_id,))
            # ONE episode query for all seasons (was: one query per season,
            # so a 7-season series burned 7 sequential pool round-trips).
            eps_by_season: dict[int, list] = {}
            sids = [se["id"] for se in season_rows]
            if sids:
                ph = ",".join("?" for _ in sids)
                for e in await run_db(
                        db.query,
                        f"SELECT * FROM episodes WHERE season_id IN ({ph}) "
                        "ORDER BY season_id, ep_no",
                        tuple(sids)):
                    eps_by_season.setdefault(e["season_id"], []).append(e)
            seasons = []
            for se in season_rows:
                eps = []
                for e in eps_by_season.get(se["id"], []):
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
    async def deeplink(file_id: int):
        f = await run_db(db.query_one, "SELECT id FROM files WHERE id=?",
                         (file_id,))
        if not f or not bot_username:
            raise HTTPException(404, "not found")
        return {"url": f"https://t.me/{bot_username}?start=file_{file_id}"}

    @app.get("/api/stats")
    async def stats():
        return await run_db(db.stats)

    # -- admin ----------------------------------------------------------------------
    @app.get("/api/admin/overview")
    async def admin_overview(request: Request):
        _admin(request)
        return {"stats": await run_db(db.stats),
                "sources": [dict(s) for s in await run_db(db.list_sources)]}

    @app.get("/api/admin/unmatched")
    async def admin_unmatched(request: Request, limit: int = 50):
        _admin(request)
        rows = await run_db(
            db.query,
            "SELECT * FROM files WHERE title_id IS NULL ORDER BY indexed_at DESC LIMIT ?",
            (min(limit, 200),))
        return {"files": [_file_dict(f) | {"file_name": f["file_name"],
                                           "tg": [f["tg_chat_id"], f["tg_msg_id"]]} for f in rows]}

    @app.post("/api/admin/file/{file_id}/hide")
    async def admin_hide(request: Request, file_id: int):
        _admin(request)
        await run_db(db.execute, "UPDATE files SET status='hidden' WHERE id=?",
                     (file_id,))
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
        await run_db(db.execute,
                     "UPDATE titles SET title=?, norm_title=?, year=?, updated_at=? WHERE id=?",
                     (title, normalize_title(title), year, time.time(), title_id))
        return {"ok": True}

    # -- mini app static --------------------------------------------------------------
    if APP_DIR.exists():
        app.mount("/app", StaticFiles(directory=str(APP_DIR), html=True), name="app")

    @app.get("/")
    async def root():
        return {"service": "media-library", "app": "/app/", "health": "/healthz"}

    return app
