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

    #: каденція поллінгу цього банку, сек. Агрегат деяких банків (PUMB) рухається
    #: раз на кілька хвилин — нема сенсу довбити API щосекунди.
    poll_interval: float = 1.0

    @abstractmethod
    def parse_ref(self, text: str) -> str:
        """Витягти стабільний ref збору з посилання (або повернути готовий ref)."""

    async def resolve_ref(self, url: str, client: httpx.AsyncClient) -> str:
        """Розвʼязати ref зі складного посилання, за потреби через мережу.

        Дефолт — синхронний parse_ref (моно: токен прямо в URL). Банки, де ref
        видно лише після редіректу (PUMB: короткий лінк → box_id), перевизначають.
        """
        return self.parse_ref(url)

    @abstractmethod
    async def fetch_jar(self, ref: str, client: httpx.AsyncClient) -> NormalizedJar:
        """Отримати поточний агрегований стан збору через публічне API банку."""
