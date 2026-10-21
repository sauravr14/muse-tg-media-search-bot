# Optimization pass — what changed and how to deploy

## Root causes found
| Symptom | Cause | Fix |
|---|---|---|
| Mini App shows blank skeletons while indexing | Every API endpoint was `async def` but ran synchronous SQLite, on the **same event loop** as the Telegram client and the indexer. One slow query froze everything (also why Pyrogram pings/requests timed out). | Catalog endpoints are plain `def` (threadpool, 8 threads); partial indexes; TTL cache; one-request `/api/home`. |
| Duplicate cards ("No Way Home We / BI / X2 / X26 / Web") | Parser kept junk after the year (truncated tags), creating one unmatched title per variant; the enricher never merged them. | Parser cuts at the year; public lists show only TMDB-matched titles with posters; search collapses unmatched variants. |
| Enricher never retried old titles | Sweep looked at only the single oldest unmatched row and spun on it. TMDB network errors were recorded as "tried" and parked the title for **7 days**. | Per-title `tmdb_next_at` + backoff; transient errors retry in 5 min; 3 workers. |
| LLM fallback permanently "failed" | A 429 returned `None`, which was cached as `{}` = "unidentifiable" forever. | Rate limits set a cooldown and are never cached. |
| Wrong TMDB matches | Matching year added +0.35, so 0.45-similarity junk passed the 0.75 bar. | Hard 0.62 title-similarity floor first; original title also compared; year-off-by-one fallback. |
| Search 429s for everyone | Behind the Vercel rewrite all users share one IP → one 60/min bucket. | Limiter keys on `X-Forwarded-For`, 120/min. |
| Slow "Get" | `get_messages` + `copy` + warning message, sequentially. | Cached bot `file_id` → single `send_cached_media`; first delivery backfills the id; warning is sent in background. |
| Telegram reconnect loop (`msg_id is lower…`) | Not code-fixable alone: usually clock skew or a duplicate instance on the same token. | Startup clock-skew check logs a clear warning + fix command. |

## Deploy
1. Replace the code, rebuild: `docker compose up -d --build`
   (first start runs a one-time migration: new columns/indexes, FTS rebuilt with prefix index, stuck titles re-queued. Takes seconds to a minute on a big catalog.)
2. Check the logs for `clock OK` — if it warns, run `sudo timedatectl set-ntp true`.
3. Make sure **only one** instance runs for this `BOT_TOKEN` (stop old Koyeb/Render deploys).
4. In Telegram, as admin run **`/cleanup`** once. It re-parses every file with the new parser and moves files out of the junk titles; the enricher then matches/merges them in the background.
5. Optional but recommended: serve the API from your own HTTPS domain (Caddy / Cloudflare Tunnel) and set
   `<meta name="api-base" content="https://api.yourdomain.com">` in `app/webapp/index.html`; drop the Vercel rewrite hop. CORS is already enabled (`CORS_ORIGINS`, default `*`).
6. Optional: close public port 8000 once the proxy is in place (the `Invalid HTTP request` log lines are scanners).

## New / changed files
- `app/db.py` – pragmas, partial indexes, FTS prefix index, migration `_migrate_v2`
- `app/parser.py` – cut-at-year, caption merge (`merge_parsed`)
- `app/tmdb.py` – `TMDBError`, 4 concurrent requests, similarity floor
- `app/llm.py` – cooldown + transient flag
- `app/indexer.py` – enricher rewrite, caption parsing, stores `tg_file_id`
- `app/classify.py` – `has_clean`, `tg_file_id`
- `app/api.py` – threadpool endpoints, `/api/home`, cache, CORS/gzip, matched-only lists
- `app/bot.py` – fast delivery path, better search
- `app/webapp/*` – new Netflix-style UI
- `tests/` – 23 new tests (parser tail, enricher, delivery, API perf/dedupe)
