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
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
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

    def _admin(request: Request, init_data: str = "") -> int:
        init_data = init_data or request.headers.get("x-telegram-init-data", "")
        data = validate_init_data(init_data, cfg.bot_token) if init_data else None
        if not data or not cfg.is_admin(data.get("user_id")):
            raise HTTPException(403, "admin only")
        return int(data["user_id"])

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # -- public catalog -----------------------------------------------------------
    @app.get("/api/search")
    async def search(request: Request, q: str = Query("", max_length=120),
                     kind: str = "", page: int = 1, hide_cam: int = 0):
        if not limiter.check(request.client.host if request.client else "?"):
            raise HTTPException(429, "slow down")
        q = q.strip()
        if not q:
            return {"results": [], "page": page}
        terms = [t for t in q.replace('"', "").split() if t]
        match = " ".join(f'"{t}"*' for t in terms)
        per, off = 20, (max(page, 1) - 1) * 20
        try:
            rows = db.query(
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
    async def recent(kind: str = "", limit: int = 24, hide_cam: int = 0):
        limit = min(max(limit, 1), 60)
        where = " WHERE kind=? " if kind in ("movie", "series") else " WHERE 1=1 "
        rows = db.query(
            "SELECT * FROM titles" + where
            + (CLEAN_ONLY if hide_cam else "")
            # titles with posters first — keeps Recently Added looking clean
            + " ORDER BY (poster != '') DESC, last_file_at DESC LIMIT ?",
            tuple(([kind] if kind in ("movie", "series") else []) + [limit]))
        return {"results": [_title_card(t) for t in rows]}

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
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad request")
        ok, msg = await bot.deliver_to_user(int(user_id), body.get("file_id"))
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True}

    @app.get("/api/browse")
    async def browse(kind: str = "", sort: str = "recent",
                     page: int = 1, per: int = 30, hide_cam: int = 0):
        """Paginated full catalog for the Movies/Series tabs."""
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
        total = db.query_one(f"SELECT COUNT(*) c FROM titles {where}",
                             tuple(params))["c"]
        rows = db.query(
            f"SELECT * FROM titles {where}ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params + [per, (page - 1) * per]))
        return {"results": [_title_card(t) for t in rows], "page": page,
                "per": per, "total": total, "has_more": page * per < total}

    @app.get("/api/title/{title_id}")
    async def title_detail(title_id: int, hide_cam: int = 0):
        t = db.query_one("SELECT * FROM titles WHERE id=?", (title_id,))
        if not t:
            raise HTTPException(404, "not found")
        out = _title_card(t)
        out.update({"orig_title": t["orig_title"], "backdrop": t["backdrop"],
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
            seasons = []
            for se in db.query("SELECT * FROM seasons WHERE title_id=? ORDER BY season_no",
                               (title_id,)):
                eps = []
                for e in db.query("SELECT * FROM episodes WHERE season_id=? ORDER BY ep_no",
                                  (se["id"],)):
                    files = db.query(
                        "SELECT * FROM files WHERE episode_id=? AND status='ok' "
                        + (" AND is_cam=0" if hide_cam else "")
                        + " ORDER BY " + RES_ORDER + " DESC", (e["id"],))
                    eps.append({"ep_no": e["ep_no"], "title": e["ep_title"],
                                "air_date": e["air_date"], "overview": e["overview"],
                                "versions": [_file_dict(f) for f in files]})
                seasons.append({"season_no": se["season_no"], "poster": se["poster"],
                                "overview": se["overview"], "episodes": eps,
                                "packs": [_file_dict(f) for f in db.query(
                                    "SELECT * FROM files WHERE season_id=? "
                                    "AND episode_id IS NULL AND status='ok' "
                                    + (" AND is_cam=0" if hide_cam else "")
                                    + " ORDER BY " + RES_ORDER + " DESC", (se["id"],))]})
            out["seasons"] = seasons
        return out

    @app.get("/api/deeplink/file/{file_id}")
    async def deeplink(file_id: int):
        f = db.query_one("SELECT id FROM files WHERE id=?", (file_id,))
        if not f or not bot_username:
            raise HTTPException(404, "not found")
        return {"url": f"https://t.me/{bot_username}?start=file_{file_id}"}

    @app.get("/api/stats")
    async def stats():
        return db.stats()

    # -- admin ----------------------------------------------------------------------
    @app.get("/api/admin/overview")
    async def admin_overview(request: Request):
        _admin(request)
        return {"stats": db.stats(),
                "sources": [dict(s) for s in db.list_sources()]}

    @app.get("/api/admin/unmatched")
    async def admin_unmatched(request: Request, limit: int = 50):
        _admin(request)
        rows = db.query(
            "SELECT * FROM files WHERE title_id IS NULL ORDER BY indexed_at DESC LIMIT ?",
            (min(limit, 200),))
        return {"files": [_file_dict(f) | {"file_name": f["file_name"],
                                           "tg": [f["tg_chat_id"], f["tg_msg_id"]]} for f in rows]}

    @app.post("/api/admin/file/{file_id}/hide")
    async def admin_hide(request: Request, file_id: int):
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
