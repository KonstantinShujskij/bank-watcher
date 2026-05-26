"""Доставка колбеків про зарахування (outbox-патерн).

Кредит спершу пишеться в БД як `pending`, далі цей воркер доставляє його з
ретраями та backoff. Тіло підписується HMAC-SHA256 спільним секретом (узгодити
з ncP2P). Доставка ідемпотентна на боці отримувача завдяки `event_id`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from ..config import settings
from ..db import Database

log = logging.getLogger("callbacks")

SWEEP_INTERVAL = 5.0  # як часто перевіряти чергу pending-колбеків


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sign_credit(p: dict, secret: str) -> str:
    """Підпис над канонічним рядком полів (стабільно для Python↔JS перевірки)."""
    canonical = "|".join([
        "credit", p["event_id"], p["bank"] or "", p["jar_ref"], p["card"] or "",
        str(p["amount"]), p["currency"], str(p["balance_after"]), str(p["detected_at"]),
    ])
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


class CallbackSender:
    def __init__(self, db: Database, client: httpx.AsyncClient) -> None:
        self.db = db
        self.client = client
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="callbacks")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("callback worker started")
        while not self._stop.is_set():
            try:
                for credit in await self.db.pending_callbacks(settings.callback_max_attempts):
                    await self._send(credit)
            except Exception:
                log.exception("callback sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SWEEP_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def deliver_one(self, credit_id: str) -> None:
        """Спроба негайної доставки (викликається поллером одразу після кредиту)."""
        credit = await self.db.get_credit(credit_id)
        if credit and credit["callback_status"] == "pending":
            await self._send(credit)

    async def _send(self, credit) -> None:
        jar = await self.db.get_jar(credit["jar_ref"])
        callback_url = jar["callback_url"] if jar else None
        if not callback_url:
            return

        payload = {
            "event_id": credit["id"],
            "bank": credit["bank"],
            "jar_ref": credit["jar_ref"],
            "card": credit["card"],
            "amount": credit["amount"],            # дельта, копійки
            "currency": credit["currency"],
            "balance_after": credit["balance_after"],
            "detected_at": credit["detected_at"],
        }
        # підпис над канонічним рядком полів → у тіло (ncP2P перевіряє з розпарсеного body)
        if settings.callback_secret:
            payload["signature"] = sign_credit(payload, settings.callback_secret)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Event-Id": credit["id"],
        }

        try:
            resp = await self.client.post(
                callback_url, content=body, headers=headers, timeout=settings.callback_timeout
            )
            success = 200 <= resp.status_code < 300
            err = None if success else f"HTTP {resp.status_code}"
        except Exception as exc:  # мережеві помилки, таймаути тощо
            success, err = False, str(exc)

        await self.db.mark_callback_result(
            credit["id"], success=success, error=err, max_attempts=settings.callback_max_attempts
        )
        if success:
            log.info("callback delivered %s -> %s", credit["id"], callback_url)
        else:
            log.warning("callback failed %s: %s", credit["id"], err)
