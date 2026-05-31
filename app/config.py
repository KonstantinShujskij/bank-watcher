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

    # HMAC-секрет для машинних (service-to-service) викликів від ncP2P.
    # Порожній = машинна авторизація вимкнена (лишається тільки сесійний логін).
    inbound_secret: str = os.getenv("INBOUND_SECRET", "")

    # Доступ до фронта/API (сесійний логін). Порожні значення = вхід вимкнено (deny).
    auth_user: str = os.getenv("AUTH_USER", "")
    auth_password: str = os.getenv("AUTH_PASSWORD", "")
    session_ttl_hours: int = int(os.getenv("SESSION_TTL_HOURS", "168"))

    vpn_enabled: bool = _bool("VPN_ENABLED")
    vpn_countries: list[str] = field(default_factory=lambda: _csv("VPN_COUNTRIES", "Ukraine"))
    # «знайди робочий екзит»: на старті connect→проба; робочий лишаємо.
    # Реконект — ЛИШЕ коли поллер ловить помилки (реактивно, без періодичної ротації).
    vpn_probe_jar: str = os.getenv("VPN_PROBE_JAR", "4xPDzE2tmw")  # публічна банка для перевірки
    vpn_max_attempts: int = int(os.getenv("VPN_MAX_ATTEMPTS", "8"))      # скільки екзитів перебрати в пошуку
    vpn_settle_seconds: float = float(os.getenv("VPN_SETTLE_SECONDS", "3"))  # пауза після connect перед пробою
    vpn_research_cooldown: int = int(os.getenv("VPN_RESEARCH_COOLDOWN", "60"))  # мін. інтервал між пошуками (анти-трешинг)
    vpn_fallback_direct: bool = _bool("VPN_FALLBACK_DIRECT", "true")     # нема робочого екзиту → прямий IP

    # Privat (headless-браузер): агрегат повільний + Chromium дорогий, тож рідкісний полінг
    # і обмежена паралельність (5 банок не відкривають 5 рендерерів водночас на 2 ГБ-боксі).
    privat_poll_interval: float = float(os.getenv("PRIVAT_POLL_INTERVAL", "30"))   # сек між полами банки
    # 1 = послідовно (≈750 МБ реального RAM, перевірено на боксі — безпечно поряд із money-сервісом
    # на 2 ГБ). 2 паралельні сторінки ≈ вдвічі більше RAM (тримати лише з достатнім запасом/swap).
    privat_concurrency: int = int(os.getenv("PRIVAT_CONCURRENCY", "1"))            # макс. одночасних сторінок
    privat_nav_timeout_ms: int = int(os.getenv("PRIVAT_NAV_TIMEOUT_MS", "45000"))
    privat_recycle_every: int = int(os.getenv("PRIVAT_RECYCLE_EVERY", "40"))       # recycle браузера кожні N навігацій
    privat_recycle_on_errors: int = int(os.getenv("PRIVAT_RECYCLE_ON_ERRORS", "3"))  # ...або після N помилок поспіль


settings = Settings()
