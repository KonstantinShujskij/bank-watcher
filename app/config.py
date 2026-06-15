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
    # Бекстоп: жоден полл однієї банки не може висіти довше за це → не клинить
    # увесь tick (asyncio.gather). > за найдовший легітимний privat-бутстрап (~90с).
    poll_fetch_timeout: float = float(os.getenv("POLL_FETCH_TIMEOUT", "120"))

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

    # Privat (headless-браузер, in-page pubinfo): ОДНА сесія полить усі банки легкими XHR-ами
    # (~0.1с/полл, ~330 МБ стабільно). Бутстрап сесії — одна повна навігація (~6с).
    privat_poll_interval: float = float(os.getenv("PRIVAT_POLL_INTERVAL", "5"))    # сек між полами банки
    privat_nav_timeout_ms: int = int(os.getenv("PRIVAT_NAV_TIMEOUT_MS", "45000"))  # таймаут бутстрап-навігації
    # Persistent Playwright-сторінка накопичує мережеві обʼєкти (Request/Response)
    # і RSS Chromium безмежно → періодично перествоюємо весь браузер (скидає і
    # Python-обʼєкти Playwright, і памʼять Chromium). Ціна — один ~6с ре-бутстрап.
    privat_recycle_seconds: float = float(os.getenv("PRIVAT_RECYCLE_SECONDS", "1800"))  # 30 хв
    # Таймаут на in-page pubinfo (page.evaluate) — Playwright сам цей виклик НЕ
    # таймаутить; без нього завислий браузер вішає полл-тік назавжди (інцидент 06-15).
    privat_eval_timeout: float = float(os.getenv("PRIVAT_EVAL_TIMEOUT", "30"))


settings = Settings()
