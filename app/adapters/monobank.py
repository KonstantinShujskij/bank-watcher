"""Адаптер публічного API банок monobank (send.monobank.ua).

Портовано з робочого прототипу. Важливо: API повертає лише агрегати
(`jarAmount` — усього зібрано, `jarPartWithdrawalAmount` — усього знято),
без списку транзакцій і без txId. Зарахування визначаємо як приріст `amount`.
"""
from __future__ import annotations

import base64

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .base import BankAdapter, NormalizedJar

API_URL = "https://send.monobank.ua/api/handler"


class JarError(Exception):
    pass


def _generate_pc() -> str:
    """API вимагає валідний публічний ключ p256 (поле Pc). Генеруємо один раз на процес."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.b64encode(pub.hex().encode()).decode()


class MonobankAdapter(BankAdapter):
    bank = "mono"

    def __init__(self) -> None:
        self._pc = _generate_pc()

    def parse_ref(self, text: str) -> str:
        text = (text or "").strip()
        if "/jar/" in text:
            text = text.split("/jar/", 1)[1]
        return text.split("?")[0].split("/")[0].strip()

    async def fetch_jar(self, ref: str, client: httpx.AsyncClient) -> NormalizedJar:
        payload = {"c": "hello", "clientId": ref, "previousJar": "", "referer": "", "Pc": self._pc}
        resp = await client.post(
            API_URL,
            json=payload,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": f"https://send.monobank.ua/jar/{ref}",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "errCode" in data:
            raise JarError(data.get("errText") or data.get("errCode"))
        return NormalizedJar(
            ref=ref,
            name=data.get("name") or data.get("ownerName") or ref,
            amount=int(data.get("jarAmount", 0)),
            withdrawal=int(data.get("jarPartWithdrawalAmount", 0)),
            currency=str(data.get("currency", "980")),
        )
