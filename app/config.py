"""Конфіг сервісу. Значення читаються з env (див. .env.example).

`.env` підвантажується автоматично через python-dotenv для зручності локальної розробки.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))

    db_path: str = os.getenv("DB_PATH", "bank_watcher.db")

    poll_interval: float = float(os.getenv("POLL_INTERVAL", "1.0"))
    poll_concurrency: int = int(os.getenv("POLL_CONCURRENCY", "8"))
    http_timeout: float = float(os.getenv("HTTP_TIMEOUT", "15"))

    callback_secret: str = os.getenv("CALLBACK_SECRET", "")
    callback_max_attempts: int = int(os.getenv("CALLBACK_MAX_ATTEMPTS", "10"))
    callback_timeout: float = float(os.getenv("CALLBACK_TIMEOUT", "15"))

    vpn_enabled: bool = _bool("VPN_ENABLED")
    vpn_rotate_seconds: int = int(os.getenv("VPN_ROTATE_SECONDS", "300"))
    vpn_countries: list[str] = field(default_factory=lambda: _csv("VPN_COUNTRIES", "Ukraine"))


settings = Settings()
