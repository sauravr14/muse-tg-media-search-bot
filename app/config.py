"""Environment configuration. No secrets are ever committed; see .env.example."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_ids(raw: str) -> tuple[int, ...]:
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return tuple(ids)


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    api_id: int = field(default_factory=lambda: _int("API_ID", 0))
    api_hash: str = field(default_factory=lambda: os.environ.get("API_HASH", ""))
    admin_ids: tuple[int, ...] = field(default_factory=tuple)
    sudo_ids: tuple[int, ...] = field(default_factory=tuple)
    log_channel_id: int = field(default_factory=lambda: _int("LOG_CHANNEL_ID", 0))
    tmdb_api_key: str = field(default_factory=lambda: os.environ.get("TMDB_API_KEY", ""))
    openrouter_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "liquid/lfm-2.5-2.6b:free"))
    mn_imdb_base_url: str = field(default_factory=lambda: os.environ.get("MN_IMDB_BASE_URL", "https://mn-api-imdb.vercel.app"))
    mini_app_url: str = field(
        default_factory=lambda: os.environ.get("MINI_APP_URL", "http://localhost:8000/app/").rstrip("/") + "/"
    )
    database_path: str = field(
        default_factory=lambda: os.environ.get("DATABASE_PATH", "data/catalog.db")
    )
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    # Optional signing secret for /api/stream tokens. When empty, a stable
    # value is derived from BOT_TOKEN server-side (never exposed).
    stream_secret: str = field(default_factory=lambda: os.environ.get("STREAM_SECRET", ""))

    def __post_init__(self) -> None:
        self.admin_ids = _parse_ids(os.environ.get("ADMIN_USER_IDS", ""))
        self.sudo_ids = _parse_ids(os.environ.get("SUDO_USER_IDS", ""))

    @property
    def has_llm(self) -> bool:
        return bool(self.openrouter_key)

    @property
    def has_tmdb(self) -> bool:
        return bool(self.tmdb_api_key)

    @property
    def has_mnimdb(self) -> bool:
        return bool(self.mn_imdb_base_url)

    def is_admin(self, user_id: int | None) -> bool:
        """Owner only. API admin routes and owner-only bot commands use this."""
        return bool(user_id) and user_id in self.admin_ids

    def is_owner(self, user_id: int | None) -> bool:
        return self.is_admin(user_id)

    def is_sudo(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.sudo_ids

    def is_privileged(self, user_id: int | None) -> bool:
        """Owner + sudo: operational commands only (index, addchannel...)."""
        return self.is_owner(user_id) or self.is_sudo(user_id)


cfg = Config()
