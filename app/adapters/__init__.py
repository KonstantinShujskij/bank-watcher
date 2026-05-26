"""Реєстр банк-адаптерів. Додати новий банк = реалізувати BankAdapter і register()."""
from __future__ import annotations

from urllib.parse import urlparse

from .base import BankAdapter, NormalizedJar
from .monobank import MonobankAdapter

_ADAPTERS: dict[str, BankAdapter] = {}

# Хост посилання → банк (для автовизначення, щоб ncP2P слав лише url)
_HOST_BANK: dict[str, str] = {
    "send.monobank.ua": "mono",
}


def detect_bank(url: str) -> str | None:
    """Визначити банк за хостом посилання. None, якщо хост невідомий / це голий ref."""
    host = (urlparse(url).hostname or "").lower()
    return _HOST_BANK.get(host)


def register(adapter: BankAdapter) -> None:
    _ADAPTERS[adapter.bank] = adapter


def get_adapter(bank: str) -> BankAdapter:
    try:
        return _ADAPTERS[bank]
    except KeyError:
        raise ValueError(f"Невідомий банк-адаптер: {bank!r}. Доступні: {available_banks()}")


def available_banks() -> list[str]:
    return sorted(_ADAPTERS)


# Вбудовані адаптери
register(MonobankAdapter())

__all__ = ["BankAdapter", "NormalizedJar", "register", "get_adapter", "available_banks", "detect_bank"]
