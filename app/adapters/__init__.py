"""Реєстр банк-адаптерів. Додати новий банк = реалізувати BankAdapter і register()."""
from __future__ import annotations

from .base import BankAdapter, NormalizedJar
from .monobank import MonobankAdapter

_ADAPTERS: dict[str, BankAdapter] = {}


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

__all__ = ["BankAdapter", "NormalizedJar", "register", "get_adapter", "available_banks"]
