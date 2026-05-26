"""Інтерфейс банк-адаптера. Кожен банк = окрема реалізація поверх цього ABC."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx


@dataclass
class NormalizedJar:
    ref: str
    name: str
    amount: int       # усього зібрано, копійки (монотонно росте — лише від поповнень)
    withdrawal: int   # усього знято, копійки
    currency: str     # ISO-4217 numeric, напр. "980" = UAH


class BankAdapter(ABC):
    #: коротка назва банку; має збігатися з полем `bank` у запитах
    bank: str = "base"

    @abstractmethod
    def parse_ref(self, text: str) -> str:
        """Витягти стабільний ref збору з посилання (або повернути готовий ref)."""

    @abstractmethod
    async def fetch_jar(self, ref: str, client: httpx.AsyncClient) -> NormalizedJar:
        """Отримати поточний агрегований стан збору через публічне API банку."""
