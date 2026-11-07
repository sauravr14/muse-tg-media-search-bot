"""Movie/show requests: store, cap, and close out user requests.

A user taps Request in the Mini App (or sends /request in the bot DM) and
the bot stores one row per request. An admin later closes it with
/done <code> and the requesting user is DM'd.

Rules (Saurav, 2026-11-07):
- Force-subscribe is checked fresh (never from the membership cache) at
  every step of the request flow.
- 5 requests per user per calendar day (IST midnight reset).
- Codes are short (6 chars, unambiguous alphabet) so an admin can type
  /done <code> from the log line.
"""
from __future__ import annotations

import random
import time

REQUESTS_PER_DAY = 5
CODE_LEN = 6
# No 0/O, 1/I/L: unambiguous when read off a log line and typed back.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
IST_OFFSET_S = 5 * 3600 + 30 * 60  # IST = UTC+5:30


class DailyCapReached(Exception):
    """User already used their REQUESTS_PER_DAY requests today (IST)."""


def day_start_ist(now: float | None = None) -> float:
    """Epoch seconds of the current IST calendar-day start."""
    now = time.time() if now is None else now
    ist = now + IST_OFFSET_S
    return (ist // 86400) * 86400 - IST_OFFSET_S


def generate_code(rng: random.Random | None = None) -> str:
    rng = rng or random
    return "".join(rng.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def count_requests_today(db, user_id: int, now: float | None = None) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM requests "
        "WHERE user_id=? AND created_at>=?",
        (user_id, day_start_ist(now)))
    return int(row["n"]) if row else 0


def create_request(db, user_id: int, username: str, first_name: str,
                   title: str, now: float | None = None) -> dict:
    """Insert one request row. Raises DailyCapReached at the cap.

    Count + insert run in one transaction so two simultaneous requests
    cannot both slip under the cap.
    """
    now = time.time() if now is None else now
    title = (title or "").strip()[:200]
    if not title:
        raise ValueError("empty request title")
    with db.transaction():
        if count_requests_today(db, user_id, now) >= REQUESTS_PER_DAY:
            raise DailyCapReached()
        for _ in range(5):
            code = generate_code()
            try:
                db.execute(
                    "INSERT INTO requests "
                    "(code, user_id, username, first_name, title, status,"
                    " created_at) VALUES (?,?,?,?,?,'open',?)",
                    (code, user_id, username or "", first_name or "",
                     title, now))
                break
            except Exception as e:
                # UNIQUE(code) collision: retry with a fresh code.
                if "UNIQUE" not in str(e):
                    raise
        else:
            raise RuntimeError("could not allocate a request code")
    return get_request_by_code(db, code)


def get_request_by_code(db, code: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM requests WHERE code=?", ((code or "").strip().upper(),))
    return dict(row) if row else None


def mark_request_done(db, code: str, now: float | None = None) -> dict | None:
    """Mark an open request done. Returns None when the code is unknown.

    The returned dict carries already_done=True when the request was
    closed before, so /done can answer honestly instead of notifying
    the user a second time.
    """
    now = time.time() if now is None else now
    row = get_request_by_code(db, code)
    if not row:
        return None
    if row["status"] == "done":
        return {**row, "already_done": True}
    db.execute(
        "UPDATE requests SET status='done', done_at=? WHERE code=?",
        (now, row["code"]))
    return {**row, "status": "done", "done_at": now, "already_done": False}
