"""Owner features: settings, force-subscribe, users/chats tracking,
broadcast recipients, and live logs.

Pure helpers live here so they are testable without a live bot client.
Bot/API code only wires them to Pyrogram/FastAPI. No secrets here: the
live-logs channel id and sudo list come from env/kv, never hardcoded.
"""
from __future__ import annotations

import html
import json
import logging
import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("mediabot.owner")

KV_START_PHOTO = "settings:start_photo"
KV_START_TEXT = "settings:start_text"
KV_FSUB = "settings:fsub"
KV_LOG_CHANNEL = "settings:log_channel"
KV_SUDO_IDS = "settings:sudo_ids"
LOG_TEXT_MAX = 300
USERS_PAGE = 25

DEFAULT_START_TEXT = (
    "🎬 <b>Core TG Media Library</b>\n\n"
    "Search movies & series from our channels.\n"
    "Just type a name, or use:\n"
    "/search &lt;name&gt; · /recent · /movies · /series · /help"
)

JOINED_STATUSES = {"member", "administrator", "creator", "restricted"}
NOT_JOINED_STATUSES = {"left", "kicked", "banned"}


def _status_name(member) -> str:
    """Normalise a ChatMember status (enum or plain string) to a word."""
    s = getattr(member, "status", "") if member is not None else ""
    if hasattr(s, "value"):
        s = s.value
    return str(s).lower().split(".")[-1]


def parse_chat_arg(text: str):
    """'@username' or '-100...' id -> str username or int id, else None."""
    t = (text or "").strip().lstrip("@")
    if not t:
        return None
    if t.lstrip("-").isdigit():
        return int(t)
    if all(c.isalnum() or c == "_" for c in t) and len(t) >= 3:
        return t
    return None


# -- settings (kv) ---------------------------------------------------------------
def fsub_list(db) -> list:
    try:
        raw = db.get_kv(KV_FSUB, "")
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def set_fsub(db, entries: list) -> None:
    db.set_kv(KV_FSUB, json.dumps(entries, ensure_ascii=False))


def fsub_invite(entry: dict) -> str:
    if entry.get("invite_link"):
        return entry["invite_link"]
    if entry.get("username"):
        return f"https://t.me/{entry['username']}"
    return ""


def get_start_photo(db) -> str:
    return db.get_kv(KV_START_PHOTO, "")


def set_start_photo(db, file_id: str) -> None:
    db.set_kv(KV_START_PHOTO, file_id or "")


def get_sudo_ids(db) -> set[int] | None:
    """Sudo admin ids from kv. Returns None if never set (caller seeds from env)."""
    raw = db.get_kv(KV_SUDO_IDS, "")
    if not raw:
        return None
    try:
        return {int(x) for x in json.loads(raw)}
    except (ValueError, TypeError):
        return set()


def set_sudo_ids(db, ids) -> None:
    db.set_kv(KV_SUDO_IDS, json.dumps(sorted(int(x) for x in ids)))


def get_start_text(db) -> str:
    return db.get_kv(KV_START_TEXT, "") or DEFAULT_START_TEXT


def set_start_text(db, text: str) -> None:
    db.set_kv(KV_START_TEXT, text or "")


def resolve_log_channel(db, env_log_channel: int) -> int:
    """kv wins over env. kv '0' means explicitly disabled."""
    kv = db.get_kv(KV_LOG_CHANNEL, "")
    if kv.strip() == "0":
        return 0
    if kv.strip():
        try:
            return int(kv.strip())
        except ValueError:
            return int(env_log_channel or 0)
    return int(env_log_channel or 0)


def set_log_channel(db, channel_id: int) -> None:
    db.set_kv(KV_LOG_CHANNEL, str(int(channel_id)))


# -- live logs -------------------------------------------------------------------
def format_log_text(text: str) -> str:
    return (text or "")[:LOG_TEXT_MAX]


def should_log_user(user_id, is_privileged: bool) -> bool:
    return bool(user_id) and not is_privileged


def format_log_message(ts: float, user_id: int, username: str,
                       name: str, text: str) -> str:
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))
    who = f"@{username}" if username else html.escape(name or "unknown")
    return (
        f"📋 <b>Live log</b> — {when}\n"
        f"👤 {html.escape(name or '')} ({who}) · <code>{user_id}</code>\n"
        f"💬 {html.escape(format_log_text(text))}"
    )


# -- force subscribe ---------------------------------------------------------------
_FSUB_CACHE: dict = {}        # (chat_id, user_id) -> (joined: bool, expires_at)
_FSUB_JOINED_TTL = 300.0       # joined users are re-checked every 5 min
_FSUB_LEFT_TTL = 5.0           # not-joined: re-check fast so "I've joined" works


def fsub_forget(user_id: int) -> None:
    for k in [k for k in _FSUB_CACHE if k[1] == user_id]:
        _FSUB_CACHE.pop(k, None)


async def _check_one(client, cid, user_id: int) -> bool:
    """True if joined (or unverifiable -> fail open). Cached."""
    now = time.monotonic()
    hit = _FSUB_CACHE.get((cid, user_id))
    if hit and hit[1] > now:
        return hit[0]
    joined = True
    ttl = _FSUB_JOINED_TTL
    try:
        member = await client.get_chat_member(cid, user_id)
        if _status_name(member) in NOT_JOINED_STATUSES:
            joined, ttl = False, _FSUB_LEFT_TTL
    except Exception as e:
        if type(e).__name__ == "UserNotParticipant":
            joined, ttl = False, _FSUB_LEFT_TTL
        else:
            log.warning("fsub check failed for %s: %r", cid, e)
            ttl = 30.0
    if len(_FSUB_CACHE) > 20000:
        _FSUB_CACHE.clear()
    _FSUB_CACHE[(cid, user_id)] = (joined, now + ttl)
    return joined


async def unjoined_channels(client, user_id: int, entries: list,
                            is_privileged: bool = False) -> list:
    """Channels from `entries` the user has NOT joined.

    Fail OPEN on any check error other than UserNotParticipant: a
    channel the bot cannot verify must never block every user.
    """
    if is_privileged or not entries:
        return []
    import asyncio
    todo = [e for e in entries if e.get("chat_id")]
    results = await asyncio.gather(
        *(_check_one(client, e["chat_id"], user_id) for e in todo))
    return [e for e, ok in zip(todo, results) if not ok]


def fsub_join_keyboard(entries: list) -> InlineKeyboardMarkup:
    rows = []
    for entry in entries:
        link = fsub_invite(entry)
        label = f"📢 Join {entry.get('title') or entry.get('chat_id')}"
        if link:
            rows.append([InlineKeyboardButton(label, url=link)])
        else:
            rows.append([InlineKeyboardButton(label, callback_data="fsub_check")])
    rows.append([InlineKeyboardButton("✅ I've joined — check again",
                                      callback_data="fsub_check")])
    return InlineKeyboardMarkup(rows)


# -- start card --------------------------------------------------------------------
def build_start_keyboard(app_button, is_owner: bool) -> InlineKeyboardMarkup:
    rows = [[app_button("🌐 Open Media Library")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help"),
             InlineKeyboardButton("😊 About", callback_data="about")]]
    if is_owner:
        rows.append([InlineKeyboardButton("⚙️ Settings", callback_data="settings:main")])
    return InlineKeyboardMarkup(rows)


# -- broadcast recipients ------------------------------------------------------------
def collect_broadcast_recipients(db, log_channel_id: int) -> list:
    """Users + group/supergroup chats + channels the bot can post in.

    Channels come from both recorded chats and enabled sources (the bot
    is admin in source channels, so it can post there). Deduplicated,
    log channel excluded, never the bot itself (it has no user row).
    """
    seen: set = set()
    out: list = []
    for row in db.query("SELECT user_id FROM users ORDER BY user_id"):
        uid = row["user_id"]
        if uid and uid != log_channel_id and uid not in seen:
            seen.add(uid)
            out.append(uid)
    for row in db.query("SELECT chat_id, type FROM chats ORDER BY chat_id"):
        cid = row["chat_id"]
        if cid and cid != log_channel_id and cid not in seen:
            seen.add(cid)
            out.append(cid)
    for row in db.query("SELECT chat_id FROM sources WHERE enabled=1"):
        cid = row["chat_id"]
        if cid and cid != log_channel_id and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out
