# Repository Analysis — Telegram Media Search Bot + Mini App

Date: 2026-10-15. Mandatory pre-build study. **Read-only: no code was copied from either repo into this project.**
Clean-room pattern study only.

---

## 1. Repo A — `sauravr14/file-store-video-bot` (private)

EvaMaria-lineage Telegram auto-filter + file-store bot. Python, Pyrogram (MTProto) plugin architecture.

- **Entry:** `bot.py` → `class Bot(Client)`: `workers=50`, `sleep_threshold=5`, `plugins={"root": "plugins"}`; starts aiohttp web server on `$PORT` for file streaming; `__main__` auto-restart loop on crash.
- **Multi-client:** `leviibot/__init__.py` (`_LeviiClient`, `LeviiBot` singleton, `multi_clients`/`work_loads` registries), `leviibot/clients.py::initialize_clients()` — extra bot tokens parsed from env (`util/config_parser.py::TokenParser`) for streaming load distribution.
- **DB:** MongoDB via motor + umongo. `database/ia_filterdb.py` — `Media` document: `_id=file_id`, `file_ref`, `file_name` (normalized at save: strips `@tag`, replaces `_ ( ) - . +` with spaces), `file_size`, `file_type`, `mime_type`, `caption`, `duration`; text index `$file_name`; `save_file()` dedups via `DuplicateKeyError`; `unpack_new_file_id()` decodes `FileId` and re-packs `(file_type, dc_id, media_id, access_hash)` so stored IDs survive restarts. `database/users_chats_db.py` — users, chats, bans, requests, `not_found_logs`, `group_activity`.
- **Channel indexing:** `plugins/index.py::index_files_to_db(lst_msg_id, chat, msg, bot)` — iterates history with `get_messages` in batches of 200 (custom `iter_messages` in `bot.py`), `save_file()` per media message; triggered by callback `index#...` after admin forwards a message/channel link in private; admin-gated, cancellable, `asyncio.Lock`-guarded.
- **File delivery:** `plugins/genlink.py` — `/link` on replied media → `https://t.me/{bot}?start={base64url('file_'+file_id)}`; deep-link start handler re-sends by stored file_id. `route.py` + `util/custom_dl.py::ByteStreamer` — HTTP range streaming at `/watch/{hash}{id}`, hash = `file_unique_id[:6]`, inline disposition for video/audio.
- **Search UX:** `plugins/pm_filter.py` (~120 KB), `pm_filter_search.py` (~49 KB) — paginated inline-keyboard results, spell-check suggestions; `plugins/inline.py` — inline queries → `InlineQueryResultCachedDocument`.
- **Config:** `info.py` — ~60 env vars (`API_ID`, `API_HASH`, `BOT_TOKEN`, `DATABASE_URI`, `DATABASE_NAME`, `COLLECTION_NAME`, `ADMINS`, `CHANNELS`, `LOG_CHANNEL`, `AUTH_CHANNEL`, verify/shortlink/premium settings…).
- **Deploy:** `Dockerfile` (python:3.11-slim + ffmpeg + deno for yt-dlp), `docker-compose.yml`, `render.yaml`, `railway.json`, `app.json`, `Procfile`, `heroku.yml`, plus per-platform `*_DEPLOYMENT_GUIDE.md`.
- **License file:** GPL-2.0.

### Notable issues / limitations
- `.env` with **real secrets is committed** to the repo — anything in it should be rotated.
- MongoDB Atlas required; no SQLite path. 30+ unpinned deps (`pyrogram`, `tgcrypto` needs compiler, `opencv`, `yt-dlp`, `googletrans`…).
- No title/year/quality parsing — stores raw normalized filenames only; search is Mongo text/regex, no fuzzy ranking, no TMDB.
- `plugins/inline.py` imports `from pyrogram import emoji` (removed in modern Pyrogram — broken).
- `utils.py` (31 KB) and `pm_filter.py` (120 KB) are god-files; hard to maintain or reuse.

## 2. Repo B — `weebzone/Telegram-Stremio` (public, GPL-3.0)

Self-hosted "Telegram channels → Stremio" media server. Python, **FastAPI + MongoDB (motor) + PyroFork**, `uv` package manager.

- **Entry:** `Backend/pyrofork/bot.py` — `StreamBot` PyroFork client (bot token, `workers=6`, `max_concurrent_transmissions=10`); optional userbot built at runtime from session string (`build_userbot`); multi-token streaming clients (`Backend/pyrofork/clients.py`).
- **DB:** `Backend/helper/database.py` (~2766 lines, motor/MongoDB). Core schema: **one doc per title** — `movies` collection (`tmdb_id`, `imdb_id`, `title`, `release_year`, `poster`, `backdrop`, `logo`, `genres`, `cast`, `runtime`, `telegram=[QualityDetail{quality, id, name, size, group_key, parts[]}]`); `tv` collection with `seasons[] → episodes[] → telegram[]`. `insert_media()` builds a `QualityDetail` and upserts: same TMDB id merges qualities (dedup + resolution grouping); same show/season/episode merges episodes.
- **Channel ingest:** `Backend/pyrofork/plugins/receiver.py` — on new channel post: `_extract_fields()` (file, caption/file_name, channel, msg_id, size), `_is_supported_media()` filter, `file_queue` + `process_file()` worker (`db_lock` serialized inserts), duplicate → `delete_message()` from channel; then catalog sync + new-media announce. `Backend/helper/scan_manager.py` — resumable backfill: 200-msg batches, 0.5 s delay, concurrency 8, cursor state in `scan_state` collection, 1 M id cap.
- **Filename parsing** (`Backend/helper/metadata/parse.py` — key patterns, described not copied):
  - `parse_media_name()`: PTN (`parse-torrent-title`) first, guessit fallback with **explicit-anchor guard** — guessit's season/episode accepted only if the name independently contains `SxxExx`, `NxNN`, or spelled-out "Season/Series N" (prevents digit-splitting hallucinations like "One Piece 1172" → S11E72).
  - Resolution disambiguation: explicit `NNNp` always wins; bare `720` counts as quality only as a trailing token fallback (else it would eat absolute episode numbers 240/480/720).
  - Absolute-episode extraction for anime after stripping extension, `[ReleaseGroup]`, quality/codec tokens, years.
  - Multipart/split/combined-season-pack detection (`split_files.py`).
- **TMDB** (`Backend/helper/metadata/providers/tmdb.py` + `resolvers.py`): `themoviedb` aioTMDb client; provider priority chains (movies TMDB>Cinemeta, series TVDB>Cinemeta>TMDB, anime Kitsu>TVDB>TMDB>Cinemeta); `score_candidate` fuzzy match vs `TMDB_THRESHOLD`; all calls via `cached_call` + `API_SEMAPHORE(12)` + in-flight Future dedup; posters via `format_tmdb_image(path, "w300")`.
- **Stream IDs:** `Backend/helper/encrypt.py` — `encode_string` = JSON → zlib → base62 (compact opaque IDs carrying channel+msg_id).
- **Config:** `sample_config.env` — `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `DATABASE` (2 URIs), `PORT`.
- **Deploy:** `Dockerfile` (uv, debian-slim), `render.yaml` (docker runtime), `docker-compose.yaml`, `heroku.yml`.

### Notable issues / limitations
- MongoDB-only; very heavyweight (subscriptions/payments, multi-DB, catalogs) — far more than a search bot needs.
- Stremio-protocol oriented (catalog/stream JSON); no Telegram chat search UI, no Mini App.
- Requires userbot session for private channels; bot-token-only works for channels where the bot is admin.

---

## 3. Reusable modules from file-store-video-bot (paths/functions, patterns only)

| Module | What to learn from |
|---|---|
| `util/file_properties.py` | `get_media_from_message()`, `parse_file_id()`, `parse_file_unique_id()`, `get_file_ids()`, `get_hash()` (unique_id[:6]), `get_name()`, `get_media_file_size()` — media extraction helpers |
| `database/ia_filterdb.py` | `Media` schema concept; `save_file()` filename normalization; `unpack_new_file_id()` restart-safe file-id packing |
| `plugins/index.py` | `index_files_to_db()` backfill loop (batches of 200, FloodWait-aware), admin/cancel/lock workflow |
| `util/custom_dl.py` | `ByteStreamer` range-request streaming pattern |
| `route.py` | Hash-gated `/watch/{hash}{id}` URL scheme |
| `info.py` | Env parsing helpers (`is_enabled`, `parse_single`, `fix_channel_id`) |
| `Dockerfile`, `render.yaml`, `docker-compose.yml` | Deployment templates for VPS/Koyeb/Render |

## 4. Useful patterns from Telegram-Stremio (clean-room descriptions)

1. **Channel indexing:** queue + single DB-writer worker (`receiver.py::process_file`); backfill with persisted cursors (`scan_manager.py`).
2. **Filename parsing/normalization:** PTN-first, guessit-only-with-explicit-anchors; strip `[ReleaseGroup]`, quality/codec/audio tokens, years before episode extraction.
3. **Title/year detection:** year regex `(19|20)\d{2}`; title = leftover after token stripping; TMDB search with year, retry without.
4. **Resolution classification:** explicit `480p/720p/1080p/2160p` tokens; bare trailing numbers only as fallback.
5. **Series/season/episode grouping:** parse S/E → nested seasons/episodes; absolute-episode fallback for anime; one DB doc per TMDB id with qualities appended.
6. **TMDB matching:** search → fuzzy `score_candidate` vs threshold → details → cache; semaphore-limited + in-flight dedup; poster/backdrop/logo URLs.
7. **Dedup:** same title+quality → skip and delete duplicate channel message; qualities merge into existing title doc.
8. **Admin workflows:** manual-add by message link, skip-channel routing, fix-wrong-metadata flow.

## 5. What must be built from scratch

- Telegram **Mini App SPA** (vanilla JS): recent-additions grid with posters, search UI, title detail (quality picker), episode browser.
- **FastAPI backend**: `/api/search`, `/api/recent`, `/api/title/{id}`, `/api/episodes`, initData validation (WebApp signature check with bot token).
- **SQLite schema**: `titles` (tmdb_id, type, title, year, poster…), `qualities` (title_id, quality, chat_id, msg_id, file_id, size), `episodes` (title_id, season, episode, quality…), `tmdb_cache`, `index_state` (per-channel cursors), full-text search (FTS5 on normalized titles).
- **Bot-token PyroFork indexer**: `get_chat_history` on admin channels (bot must be admin — Bot API allows history reads there), new-post handler, `/index`, `/search`, `/stats` commands, deep-link `?start=` file delivery via `copy_message`.
- TMDB client over plain `httpx` with server-side SQLite caching (no `themoviedb` lib needed).
- Clean-room filename parser (PTN + own regexes; see §7).

## 6. Compatibility / dependency conflicts

- fsbot: `pyrogram` (unpinned, needs `tgcrypto` compiler toolchain) + `pyromod`. Stremio: `pyrofork>=2.3.61` + `uv`. **New project: `pyrofork==2.3.69`** (matches the user's working copy-bot; no tgcrypto, no compiler).
- fsbot `requirements.txt` is 30+ heavy deps (opencv, yt-dlp, deno) — none needed for search bot; skip.
- Stremio uses `uv`/lockfile; new project uses plain `pip` + `requirements.txt` (simpler for Koyeb/Render/VPS parity).
- DB: both repos MongoDB-only → new project uses **SQLite (aiosqlite)** + FTS5: zero external DB, works on Koyeb/Render free tiers and VPS alike.
- Filename parsing libs: `parse-torrent-title` (PTN, MIT) + `rapidfuzz` (MIT) are safe; `guessit` is LGPL-3.0 — usable as a library without copylefting our code, but optional; own regexes cover the required cases.

## 7. Security and licensing notes

- **Telegram-Stremio is GPL-3.0; file-store-video-bot's LICENSE is GPL-2.0.** We studied both read-only and **copied no code**; the new repo is a clean-room implementation of observed *ideas/patterns* (indexing flow, parsing strategy, schema shape), which does not trigger copyleft. **The new repo can therefore be MIT-licensed.** (Extra reason not to mix: GPL-2.0 and GPL-3.0 are mutually incompatible anyway.)
- fsbot's repo **commits a real `.env`** — rotate any secrets found there; new repo must `.gitignore` `.env` and use platform env vars.
- Mini App: validate Telegram `initData` HMAC on every API call; never trust client-supplied user ids.
- TMDB API key stays server-side; cache posters/metadata in SQLite, serve through our API.
- Bot token / API hash only in env; admin commands gated by `OWNER_ID`; rate-limit `/api/search`.

## 8. Proposed final architecture

```
Telegram channels (bot admin)
        │  PyroFork bot-token client (get_chat_history + new-post handler)
        ▼
┌──────────────┐   ┌──────────────┐   ┌───────────────────┐
│ indexer.py   │──▶│ SQLite (FTS5)│◀──│ api.py (FastAPI)  │
│ parse.py     │   │ titles/      │   │ /api/search etc.  │
│ tmdb.py      │──▶│ qualities/   │   │ initData validation│
└──────────────┘   │ episodes/    │   └────────┬──────────┘
                   │ tmdb_cache   │            │ JSON
                   └──────────────┘            ▼
                                        Mini App SPA (vanilla JS)
                                        poster grid · search · quality picker
Bot chat: /search <q>, /recent, /index, WebApp button → Mini App
File delivery: ?start=<opaque_id> deep link → copy_message from source channel
```

- **Stack:** Python 3.12, `pyrofork==2.3.69`, `fastapi`, `uvicorn`, `aiosqlite`, `httpx`, `parse-torrent-title`, `rapidfuzz`, `python-dotenv`.
- **Deploy:** single `Dockerfile` (python:3.12-slim, no compiler toolchain), `docker-compose.yml` (VPS), `render.yaml`, Koyeb-ready (`$PORT` binding). SQLite file on a mounted volume.

## 9. File-by-file implementation plan

```
tg-media-search-bot/
├── bot/
│   ├── __init__.py
│   ├── config.py        # env parsing (API_ID, API_HASH, BOT_TOKEN, OWNER_ID, TMDB_API_KEY, CHANNELS, DATABASE_PATH, PORT)
│   ├── client.py        # PyroFork bot-token client singleton
│   ├── indexer.py       # channel backfill (cursor per channel) + new-post handler → db
│   ├── parse.py         # clean-room filename parser: title/year/quality/SxxExx/absolute-ep (PTN + own regexes)
│   ├── tmdb.py          # httpx TMDB client: search→match(threshold)→details, SQLite-cached, semaphore-limited
│   ├── db.py            # aiosqlite schema, FTS5, CRUD: upsert_title, add_quality, search, recent
│   ├── dedup.py         # same title+quality+size → skip; grouping rules (movie=one title, series=S/E tree)
│   ├── handlers.py      # /start (deep-link delivery), /search, /recent, /index, /stats, WebApp button
│   └── miniapp_auth.py  # initData HMAC validation
├── api/
│   ├── __init__.py
│   └── routes.py        # FastAPI: /api/search?q=, /api/recent, /api/title/{id}, /api/health
├── webapp/
│   ├── index.html       # SPA shell
│   ├── app.js           # search, recent grid, title detail, quality/episode picker (vanilla JS)
│   └── style.css
├── Dockerfile           # python:3.12-slim, pip install, CMD uvicorn+bot
├── docker-compose.yml   # VPS: app + volume for sqlite
├── render.yaml          # Render web service (docker)
├── requirements.txt     # pinned: pyrofork==2.3.69, fastapi, uvicorn, aiosqlite, httpx, parse-torrent-title, rapidfuzz, python-dotenv
├── .env.example         # documented env template (no secrets)
├── .gitignore           # .env, *.db, __pycache__
├── LICENSE              # MIT
└── README.md            # setup, VPS/Koyeb/Render deploy, admin commands
```

Build order: `config.py` → `db.py` → `parse.py` → `tmdb.py` → `indexer.py` → `handlers.py` → `api/routes.py` → `webapp/` → Docker/deploy files.
