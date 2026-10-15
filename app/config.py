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


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    api_id: int = field(default_factory=lambda: _int("API_ID", 0))
    api_hash: str = field(default_factory=lambda: os.environ.get("API_HASH", ""))
    admin_ids: tuple[int, ...] = field(default_factory=tuple)
    tmdb_api_key: str = field(default_factory=lambda: os.environ.get("TMDB_API_KEY", ""))
    mini_app_url: str = field(
        default_factory=lambda: os.environ.get("MINI_APP_URL", "http://localhost:8000/app/").rstrip("/") + "/"
    )
    database_path: str = field(
        default_factory=lambda: os.environ.get("DATABASE_PATH", "data/catalog.db")
    )
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        raw = os.environ.get("ADMIN_USER_IDS", "")
        ids = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.append(int(part))
        self.admin_ids = tuple(ids)

    @property
    def has_tmdb(self) -> bool:
        return bool(self.tmdb_api_key)

    def is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.admin_ids


cfg = Config()
