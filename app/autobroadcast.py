"""Scheduled auto-broadcast: every N hours, post one fresh trending title
with its TMDB poster + Mini App button, never repeating a title.

Selection (2026-11-13, Saurav's spec): recently released AND recently
added to the Mini App catalog, with a thumbnail. Ranked: recent release
years first (year >= 2024), then TMDB popularity — so a 2026 hit beats a
2005 evergreen that merely got a new episode file. A title is picked at
most once, ever (auto_broadcast_posts), and a cycle with no fresh poster
title is skipped silently instead of repeating.

Settings live in kv `settings:auto_broadcast` as JSON:
{"enabled": true, "interval_hours": 12, "scope": "all",
 "last_post_at": 0.0, "last_title_id": null}
Defaults are ON / 12h / everyone, per Saurav's "on by default" call.
"""
from __future__ import annotations

import html
import json
import time

KV_KEY = "settings:auto_broadcast"
DEFAULT_INTERVAL_HOURS = 12
INTERVAL_OPTIONS = (6, 12, 24)
FRESH_WINDOW_DAYS = 7
RECENT_YEAR = 2024


def defaults() -> dict:
    return {"enabled": True, "interval_hours": DEFAULT_INTERVAL_HOURS,
            "scope": "all", "last_post_at": 0.0, "last_title_id": None}


def get_settings(db) -> dict:
    st = defaults()
    try:
        raw = db.get_kv(KV_KEY)
        if raw:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                st.update({k: v for k, v in saved.items() if k in st})
    except Exception:
        pass
    if st["interval_hours"] not in INTERVAL_OPTIONS:
        st["interval_hours"] = DEFAULT_INTERVAL_HOURS
    if st["scope"] not in ("all", "users"):
        st["scope"] = "all"
    return st


def set_settings(db, patch: dict) -> dict:
    st = get_settings(db)
    st.update({k: v for k, v in patch.items() if k in st})
    db.set_kv(KV_KEY, json.dumps(st))
    return st


def next_interval(hours: int) -> int:
    """Cycle 6h -> 12h -> 24h -> 6h (the panel's interval button)."""
    try:
        i = INTERVAL_OPTIONS.index(int(hours))
    except ValueError:
        return DEFAULT_INTERVAL_HOURS
    return INTERVAL_OPTIONS[(i + 1) % len(INTERVAL_OPTIONS)]


def pick_title(db, now: float | None = None):
    """Freshest trending poster title never auto-posted before, or None.

    Fresh = new files in the last FRESH_WINDOW_DAYS. Recent release years
    rank first, then TMDB popularity, then newest files.
    """
    now = time.time() if now is None else now
    cutoff = now - FRESH_WINDOW_DAYS * 86400
    rows = db.query(
        "SELECT * FROM titles "
        "WHERE poster != '' AND tmdb_id IS NOT NULL "
        "AND last_file_at >= ? "
        "AND id NOT IN (SELECT title_id FROM auto_broadcast_posts) "
        f"ORDER BY (year >= {RECENT_YEAR}) DESC, "
        "popularity DESC NULLS LAST, last_file_at DESC LIMIT 1",
        (cutoff,))
    return dict(rows[0]) if rows else None


def record_post(db, title_id: int, posted_at: float, scope: str = "all",
                source: str = "auto") -> None:
    db.execute(
        "INSERT OR IGNORE INTO auto_broadcast_posts"
        "(title_id, posted_at, scope, source) VALUES (?, ?, ?, ?)",
        (title_id, posted_at, scope, source))


def was_posted(db, title_id: int) -> bool:
    row = db.query_one("SELECT title_id FROM auto_broadcast_posts WHERE title_id=?",
                       (title_id,))
    return row is not None


def _kind_label(kind: str) -> str:
    return "TV show" if kind == "series" else "Movie"


def build_caption(row: dict) -> str:
    """Template caption, close to Saurav's example:
    'Watch trending TV show <b>Carrie</b> (2026) available on the bot'."""
    title = html.escape(row["title"])
    year = f" ({row['year']})" if row.get("year") else ""
    rating = row.get("rating")
    lines = [
        f"🔥 Watch trending {_kind_label(row.get('kind', 'movie'))} "
        f"<b>{title}</b>{year} — now available on the bot!",
    ]
    if rating:
        lines.append(f"⭐ {float(rating):.1f}/10")
    return "\n".join(lines)
