# 🎬 Telegram Media Search Bot + Mini App

A centralized media discovery platform for movies & TV series stored across your
Telegram channels. Indexes channel media into a local catalog, groups qualities
(480p/720p/1080p) under one movie card and episodes under seasons, fetches
posters from TMDB, and serves it all through a Telegram bot **and** a Mini App
web UI that opens inside Telegram.

## Features

- **Channel indexing** — historical backfill (resumable, idempotent) + live indexing of new posts. Bot-token only, no user session needed.
- **Smart title cleanup** — the filename parser strips release tags, codecs, site names, audio tags, hashes/UUIDs and malformed brackets (`The Pianist 2002 REMASTERED 720p 10Bit BluRay NF Engli` → `The Pianist` 2002). Titles only accepted after a TMDB score check, so junk never pollutes the catalog. An **OpenRouter LLM fallback** handles the titles the parser can't: only TMDB-rejected titles go to the LLM, every guess is re-validated against TMDB, and guesses are cached by filename hash so you never pay twice. Needs `OPENROUTER_API_KEY`; model via `LLM_MODEL` (default `liquid/lfm-2.5-2.6b:free`, which works on OpenRouter's free tier — set `openai/gpt-4o-mini` if you have paid credits).
- **TMDB metadata** — posters, backdrops, overview, genres, ratings, episode titles. Cached server-side; API key never reaches the frontend.
- **Bot search** — plain text = search; results with posters; tap a quality to get the file copied into your chat; series browser (seasons → episodes → qualities); deep links (`t.me/<bot>?start=file_<id>`).
- **Mini App** — dark-first responsive SPA: search with debounce, recently-added grids, full-catalog **Browse** (Movies/Series tabs, 30/page + Load more, 4 sort orders: Recent, A–Z, Top rated, Year), movie/series detail pages with season tabs, quality grouping and season packs. **Clean-prints toggle** (🎭 All prints → glowing ✨ Clean only, top-right + ⓘ info popup, choice remembered in localStorage) hides theatre prints — HDCAM, CAM, CAMRIP, HDTS, TS, TELESYNC, TSRIP, PreDVD, HDTC, TC — across home, search, browse and detail pages (`hide_cam=1` on the API). Opens inside Telegram; the **Get** button opens the bot deep link and closes the Mini App so the file lands in chat.
- **Admin** — bot commands (`/addchannel`, `/index`, `/stats`, `/reparse`, …) + a protected `/app/admin.html` page (retitle, hide files, review unmatched).
- **Single container** — bot + API in one process; SQLite (WAL) so there is nothing else to run. Runs on a low-memory VPS, Koyeb, or Render.

## Quick start (VPS with Docker)

```bash
git clone <this-repo> && cd tg-media-search-bot
cp .env.example .env   # fill in BOT_TOKEN, TMDB_API_KEY, ADMIN_USER_IDS, MINI_APP_URL
docker compose up -d --build
```

Then in Telegram:
1. Make the bot **admin** in every source channel.
2. `/addchannel @yourchannel` (as an admin user)
3. `/index` — scans history in the background; new posts index live.
4. Open the Mini App from `/start` → **Open Media Library**.

Set `MINI_APP_URL` to the public URL of `/app/` (e.g. `https://media.example.com/app/`).

## Environment

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather) |
| `API_ID` / `API_HASH` | yes | From [my.telegram.org](https://my.telegram.org) → API development tools (required for MTProto auth, even with a bot token) |
| `ADMIN_USER_IDS` | yes | Comma-separated Telegram user ids (bot admin commands + web admin) |
| `TMDB_API_KEY` | no | From [themoviedb.org](https://www.themoviedb.org/settings/api) — without it, posters are placeholders |
| `OPENROUTER_API_KEY` | no | For the AI title-cleanup fallback — omit to run parser + TMDB only |
| `LLM_MODEL` | no | Default `liquid/lfm-2.5-2.6b:free` (works on OpenRouter's free tier); use `openai/gpt-4o-mini` with paid credits |
| `MINI_APP_URL` | yes | Public URL of the Mini App, ending in `/app/` |
| `DATABASE_PATH` | no | Default `/app/data/catalog.db` in Docker |
| `PORT` | no | Default `8000` |

Never commit real values — `.env` is git-ignored.

## Bot commands

**Everyone:** `/start` (+ Mini App button), `/search <name>`, `/recent`, `/movies`, `/series`, `/help`, `/id` — or just type a name.

**Admins:** `/addchannel <id|@username|invite-link>`, `/rmchannel <id>`, `/channels`, `/index`, `/reparse`, `/stats`. Forward any channel post to the bot and it asks **"Index this channel? [Yes/No]"** to start a scan.

## Mini App on Vercel (HTTPS for Telegram)

Telegram only loads Mini Apps over HTTPS. Host the web UI on Vercel:

1. Push this repo to GitHub (already done).
2. Vercel → New Project → import the repo → set **Root Directory** to `app/webapp`.
3. Deploy. Your Mini App URL becomes `https://<your-app>.vercel.app/`.
4. `app/webapp/vercel.json` rewrites `/api/*` to the VPS backend
   (`http://152.67.176.107:8000`) server-side, so the SPA needs no changes
   and there are no mixed-content or CORS issues. Update the IP there if your
   VPS address changes.
5. Set `MINI_APP_URL=https://<your-app>.vercel.app/` in the VPS `.env` and
   redeploy the container — the bot then serves real Mini App buttons, and you
   can paste the same URL into BotFather → /mybots → Bot Settings → Menu Button.

## Koyeb / Render

- **Koyeb:** App → Service → GitHub repo, Dockerfile builder, port `8000`, health check `/healthz`. Set the env vars; point `MINI_APP_URL` at the Koyeb URL + `/app/`. Attach a volume at `/app/data` or the catalog resets on redeploy.
- **Render:** New → Web Service → this repo (Docker runtime), health check `/healthz`, add a 1 GB disk at `/app/data`. See `render.yaml`.

## Architecture

```
app/
  main.py      # entrypoint: PyroFork bot + uvicorn in one asyncio loop
  bot.py       # bot commands, search cards, callbacks, file delivery (msg.copy)
  indexer.py   # historical backfill (checkpointed) + live channel handler + TMDB enricher
  classify.py  # files -> canonical titles / seasons / episodes
  parser.py    # filename -> title/year/resolution/SxxEyy/codec, cam-print detection
  tmdb.py      # TMDB client, cached in DB, failures never break indexing
  llm.py       # OpenRouter title-cleanup fallback (free-tier friendly), guesses cached + TMDB-validated
  api.py       # FastAPI: /api/*, WebApp initData validation, admin API
  db.py        # SQLite schema (titles, seasons, episodes, files, sources, tmdb_cache) + FTS5
  webapp/      # Mini App SPA (index.html, app.js, styles.css) + admin.html
  config.py
```

**Why SQLite and not Postgres/Mongo:** zero extra services, survives on a 1 GB VPS,
single source of truth, and the catalog workload (metadata only, no BLOBs) fits
comfortably. Posters are hot-linked from TMDB's CDN, never downloaded.

**Telegram limits (be honest):**
- Bots cannot use `messages.GetHistory`, so history is read with
  `messages.GetMessages` over explicit ID ranges (same technique as
  EvaMaria/Levii filter bots) — no session string needed.
- The bot must be **admin** in private source channels. Public channels can be
  indexed from a forwarded post or a `t.me` post link without adding the bot.
- To index a channel: `/addchannel` it, then **forward any message** from the
  channel to the bot (or send a `t.me` post link) and tap ✅ Yes.
- File delivery uses `message.copy`, so files stay on Telegram's servers; nothing
  is downloaded by this app.

## Repository analysis

`docs/REPO_ANALYSIS.md` contains the mandatory pre-build study of
`sauravr14/file-store-video-bot` and `weebzone/Telegram-Stremio`
(patterns studied read-only; **no code was copied** — this project is original
work, MIT licensed).

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

## License

MIT — see `LICENSE`.
