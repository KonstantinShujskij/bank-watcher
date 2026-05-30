"""Адаптер PUMB-зборів (payhub.com.ua moneybox / «Скриня»).

Короткий лінк `mobile-app.pumb.ua/<code>` редіректить на
`frames*.payhub.com.ua/moneybox?box_id=<UUID>`. Стабільний ref = `box_id`.

Публічне API віддає лише агрегати (як monobank): `total_amount` — усього
зібрано (копійки, монотонно росте), без списку транзакцій і без txId.
Зарахування визначаємо як приріст `total_amount`.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx

from .base import BankAdapter, NormalizedJar

API_URL = "https://rlyeh2.payhub.com.ua/frames/donations/info"
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class JarError(Exception):
    pass


class PumbAdapter(BankAdapter):
    bank = "pumb"
    poll_interval = 15.0  # агрегат оновлюється раз на 3-5 хв → 15с достатньо

    def _box_id_from(self, text: str) -> str | None:
        """box_id з готового UUID або з URL із ?box_id=…; інакше None."""
        text = (text or "").strip()
        if _UUID_RE.match(text):
            return text
        try:
            bid = (parse_qs(urlparse(text).query).get("box_id") or [None])[0]
            if bid and _UUID_RE.match(bid):
                return bid
        except Exception:
            pass
        return None

    def parse_ref(self, text: str) -> str:
        # Синхронно дістаємо box_id лише якщо він уже в посиланні (frames-URL/UUID).
        # Для короткого mobile-app.pumb.ua/<code> потрібен resolve_ref (мережа).
        return self._box_id_from(text) or ""

    async def resolve_ref(self, url: str, client: httpx.AsyncClient) -> str:
        bid = self._box_id_from(url)
        if bid:
            return bid
        # Короткий лінк → йдемо за редіректами до frames-URL із box_id.
        resp = await client.get(
            url, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        )
        bid = self._box_id_from(str(resp.url))
        if not bid:
            for hop in resp.history:  # box_id міг бути лише в Location проміжного хопа
                bid = self._box_id_from(hop.headers.get("location", ""))
                if bid:
                    break
        if not bid:
            raise JarError(f"Не вдалося розпізнати box_id PUMB-збору з {url}")
        return bid

    async def fetch_jar(self, ref: str, client: httpx.AsyncClient) -> NormalizedJar:
        # API вимагає box_id + непорожній link_params (трекінговий; даємо ref).
        resp = await client.get(
            API_URL,
            params={"box_id": ref, "link_params": ref},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") in ("BAD_REQUEST", "NOT_FOUND"):
            raise JarError(data.get("message") or data.get("code"))
        return NormalizedJar(
            ref=ref,
            name=data.get("owner_name") or data.get("goal") or ref,
            amount=int(data.get("total_amount") or 0),  # усього зібрано, копійки
            withdrawal=0,  # PUMB не віддає окремо знятого
            currency="980",  # UAH (валюти в JSON нема)
        )
