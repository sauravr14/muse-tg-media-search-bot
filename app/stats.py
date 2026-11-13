"""Admin usage stats: downloads, plays, searches, blocks, failures.

One row in `events` per user action (see db.py). Recording is a single
INSERT at the choke point of each action, so it can never double-count:
deliver_to_user (Get File, both Mini App and bot DM), stream_token (Play),
_do_search + /api/search (searches). All aggregates are plain SQL over
that table; counters start at the deploy that introduced them.
"""
from __future__ import annotations

import time

IST_OFFSET = 5 * 3600 + 30 * 60  # Asia/Kolkata, no DST

TOP_TITLES = 10
TOP_USERS = 5
TOP_SEARCHES = 5
DAYS_SERIES = 7


def day_start(now: float) -> float:
    """Epoch of IST midnight on the day `now` falls in."""
    return (now + IST_OFFSET) // 86400 * 86400 - IST_OFFSET


def normalize_query(q: str) -> str:
    return " ".join((q or "").strip().lower().split())[:100]


def record_event(db, kind: str, user_id: int = 0, file_id: int | None = None,
                 detail: str = "", now: float | None = None) -> None:
    db.execute(
        "INSERT INTO events(kind, user_id, file_id, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, int(user_id or 0), file_id, detail or "",
         time.time() if now is None else now))


def _count(db, kind: str, since: float | None = None,
           until: float | None = None) -> int:
    sql = "SELECT COUNT(*) c FROM events WHERE kind=?"
    params: list = [kind]
    if since is not None:
        sql += " AND created_at>=?"
        params.append(since)
    if until is not None:
        sql += " AND created_at<?"
        params.append(until)
    row = db.query_one(sql, tuple(params))
    return int(row["c"]) if row else 0


def _top_titles(db, kind: str, limit: int = TOP_TITLES) -> list[dict]:
    rows = db.query(
        "SELECT t.title AS title, t.year AS year, t.kind AS kind,"
        " COUNT(*) c FROM events e"
        " JOIN files f ON f.id = e.file_id"
        " JOIN titles t ON t.id = f.title_id"
        " WHERE e.kind=? GROUP BY t.id ORDER BY c DESC, t.title LIMIT ?",
        (kind, limit))
    return [dict(r) for r in rows]


def overview(db, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    today = day_start(now)
    out: dict = {
        "downloads_total": _count(db, "download"),
        "plays_total": _count(db, "play"),
        "downloads_today": _count(db, "download", today, today + 86400),
        "plays_today": _count(db, "play", today, today + 86400),
        "downloads_7d": _count(db, "download", now - 7 * 86400, now),
        "plays_7d": _count(db, "play", now - 7 * 86400, now),
        "downloads_30d": _count(db, "download", now - 30 * 86400, now),
        "plays_30d": _count(db, "play", now - 30 * 86400, now),
        "fsub_blocks_total": _count(db, "fsub_block"),
        "delivery_fails_total": _count(db, "delivery_fail"),
        "top_downloads": _top_titles(db, "download"),
        "top_plays": _top_titles(db, "play"),
    }
    # downloads + plays per day, last 7 days (IST days, oldest first)
    series: list[dict] = []
    for i in range(DAYS_SERIES - 1, -1, -1):
        start = today - i * 86400
        end = start + 86400
        dl = db.query_one(
            "SELECT COUNT(*) c FROM events WHERE kind='download'"
            " AND created_at>=? AND created_at<?", (start, end))
        pl = db.query_one(
            "SELECT COUNT(*) c FROM events WHERE kind='play'"
            " AND created_at>=? AND created_at<?", (start, end))
        label = time.strftime("%d %b", time.gmtime(start + IST_OFFSET))
        series.append({"day": label,
                       "downloads": int(dl["c"]) if dl else 0,
                       "plays": int(pl["c"]) if pl else 0})
    out["daily"] = series
    # top users by downloads + plays
    rows = db.query(
        "SELECT e.user_id AS user_id, u.username AS username,"
        " u.first_name AS first_name, COUNT(*) c FROM events e"
        " LEFT JOIN users u ON u.user_id = e.user_id"
        " WHERE e.kind IN ('download', 'play')"
        " GROUP BY e.user_id ORDER BY c DESC, e.user_id LIMIT ?",
        (TOP_USERS,))
    out["top_users"] = [dict(r) for r in rows]
    # top search queries
    rows = db.query(
        "SELECT detail AS query, COUNT(*) c FROM events"
        " WHERE kind='search' AND detail != ''"
        " GROUP BY detail ORDER BY c DESC LIMIT ?",
        (TOP_SEARCHES,))
    out["top_searches"] = [dict(r) for r in rows]
    # quality split for downloads
    rows = db.query(
        "SELECT COALESCE(f.resolution, '?') AS resolution, COUNT(*) c"
        " FROM events e JOIN files f ON f.id = e.file_id"
        " WHERE e.kind='download' GROUP BY f.resolution ORDER BY c DESC")
    out["quality"] = [dict(r) for r in rows]
    # requests (existing table): pending = anything not done
    row = db.query_one(
        "SELECT COUNT(*) c FROM requests WHERE status='done'")
    out["requests_done"] = int(row["c"]) if row else 0
    row = db.query_one(
        "SELECT COUNT(*) c FROM requests WHERE status!='done'")
    out["requests_pending"] = int(row["c"]) if row else 0
    # users: total / new today / active today
    out["users_total"] = db.count_users()
    row = db.query_one("SELECT COUNT(*) c FROM users WHERE first_seen>=?",
                       (today,))
    out["users_new_today"] = int(row["c"]) if row else 0
    row = db.query_one("SELECT COUNT(*) c FROM users WHERE last_seen>=?",
                       (today,))
    out["users_active_today"] = int(row["c"]) if row else 0
    # catalog totals (existing stats)
    out["catalog"] = db.stats()
    return out


def totals(db) -> tuple[int, int]:
    """(total downloads, total plays) — cheap line for the Settings menu."""
    return _count(db, "download"), _count(db, "play")
