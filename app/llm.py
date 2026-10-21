"""Optional LLM fallback for title identification via OpenRouter.

Used only when the offline parser + TMDB direct match fail: the LLM sees the
raw release filename and proposes the official title, which is then
cross-checked against TMDB (the score threshold still applies, so a
hallucinated guess can't pollute the catalog).

Needs OPENROUTER_API_KEY in the environment. Model via LLM_MODEL,
default "liquid/lfm-2.5-2.6b:free" (free tier; paid models need credits).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time

import aiohttp

log = logging.getLogger("mediabot.llm")

FREE_MODEL = "liquid/lfm-2.5-2.6b:free"

SYSTEM = (
    "You identify movies and TV series from messy release filenames. "
    "Reply with ONLY a JSON object, no thinking, no other text."
)
USER_TMPL = """Filename: "{fn}"

Reply with ONLY this JSON object, no thinking, no other text:
{{"title": "Official title", "year": 1999, "kind": "movie"}}

Rules:
- "title" is the official title as listed on TMDB/IMDb (the English title when one exists).
- "year" is the release year as an integer, or null when unknown.
- "kind" is "movie" or "series".
- If you cannot identify it, reply {{"title": null}}."""


class LLTitle:
    def __init__(self, api_key: str, model: str = FREE_MODEL,
                 session: aiohttp.ClientSession | None = None):
        self.api_key = api_key
        self.model = model or FREE_MODEL
        self._session = session
        # Free tiers rate-limit hard. After a 429 we stop calling for a while
        # (no inline sleeping that stalls the enricher) and flag the result
        # as transient so callers do NOT cache "unidentifiable".
        self.cooldown_until = 0.0
        self._streak = 0
        self.transient = False

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def cache_key(self, filename: str) -> str:
        h = hashlib.md5(filename.encode("utf-8", "ignore")).hexdigest()
        return f"llm_guess:{h}"

    async def guess_title(self, filename: str) -> dict | None:
        """Return {"title", "year", "kind"} or None when unidentifiable."""
        if not self.available or not filename:
            return None
        self.transient = False
        if time.time() < self.cooldown_until:
            self.transient = True
            return None
        data = None
        try:
            s = await self._sess()
            async with s.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model,
                      "messages": [{"role": "system", "content": SYSTEM},
                                   {"role": "user",
                                    "content": USER_TMPL.format(fn=filename[:200])}],
                      "temperature": 0,
                      "max_tokens": 200},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status == 429:
                    self._streak += 1
                    wait = min(30 * self._streak, 300)
                    self.cooldown_until = time.time() + wait
                    self.transient = True
                    log.warning("openrouter rate-limited, pausing LLM calls for %ss", wait)
                    return None
                if r.status != 200:
                    log.warning("openrouter %s: %s", r.status, (await r.text())[:120])
                    self.transient = r.status >= 500
                    return None
                data = await r.json()
                self._streak = 0
        except Exception as e:
            log.warning("openrouter request failed: %r", e)
            self.transient = True
            return None
        if data is None:
            return None
        try:
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content and msg.get("reasoning"):
                # reasoning-style model: fish the JSON out of the thinking
                m = re.search(r"\{[^{}]*\"title\"[^{}]*\}", msg["reasoning"], re.S)
                content = m.group(0) if m else ""
            content = re.sub(r"^```(?:json)?|```$", "", content.strip())
            obj = json.loads(content)
        except Exception as e:
            log.warning("openrouter bad json: %r", e)
            return None
        title = (obj.get("title") or "").strip() if isinstance(obj, dict) else ""
        if not title:
            return None
        year = obj.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        kind = obj.get("kind") if obj.get("kind") in ("movie", "series") else "movie"
        return {"title": title, "year": year, "kind": kind}
