"""Поллер зборів: раз на POLL_INTERVAL опитує активні банки і фіксує зарахування.

Зарахування = приріст `amount` (усього зібрано) між опитуваннями. Дедуп —
синтетичний id від кумулятивного балансу: він монотонний, тож унікальний на
межу кредиту, і повторне зчитування того ж балансу не створює дубль.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx

from ..adapters import get_adapter
from ..config import settings
from ..db import Database
from .callbacks import CallbackSender

log = logging.getLogger("poller")


def credit_id(jar_ref: str, balance_after: int) -> str:
    return hashlib.sha256(f"{jar_ref}:{balance_after}".encode()).hexdigest()[:32]


class Poller:
    def __init__(self, db: Database, client: httpx.AsyncClient, sender: CallbackSender,
                 on_fetch_error=None) -> None:
        self.db = db
        self.client = client
        self.sender = sender
        # сигнал назовні (VPN): фетч банк-API провалився → можливо, екзит заблоковано
        self._on_fetch_error = on_fetch_error
        self._sem = asyncio.Semaphore(settings.poll_concurrency)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("poller started, interval=%.2fs", settings.poll_interval)
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                await self._tick()
            except Exception:
                log.exception("poller tick failed")
            # тримаємо стабільну каденцію ~POLL_INTERVAL
            elapsed = time.monotonic() - start
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, settings.poll_interval - elapsed))
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        jars = await self.db.list_active_jars()
        if jars:
            await asyncio.gather(*(self._poll_jar(j) for j in jars))

    async def _poll_jar(self, jar) -> None:
        async with self._sem:
            try:
                adapter = get_adapter(jar["bank"])
            except Exception as exc:
                log.warning("no adapter for %s: %s", jar["ref"], exc)
                return

            # Per-bank каденція: загальний тік ~1с, але банк із більшим
            # poll_interval (PUMB=15с) опитуємо рідше — агрегат рухається раз на
            # кілька хвилин, нема сенсу довбити API щосекунди.
            last = jar["last_polled_at"]
            if last is not None and (int(time.time() * 1000) - last) < adapter.poll_interval * 1000:
                return

            try:
                fresh = await adapter.fetch_jar(jar["ref"], self.client)
            except Exception as exc:
                await self.db.mark_jar_error(jar["ref"], str(exc))
                log.warning("fetch %s failed: %s", jar["ref"], exc)
                if self._on_fetch_error:
                    try: self._on_fetch_error()
                    except Exception: pass
                return

            delta = fresh.amount - jar["last_amount"]

            inserted = False
            cid = ""
            if delta > 0:
                # 1) спершу фіксуємо кредит (idempotent), 2) потім рухаємо знімок —
                # якщо впадемо між кроками, наступний тік повторно вставить той самий id (no-op)
                cid = credit_id(jar["ref"], fresh.amount)
                inserted = await self.db.insert_credit(
                    id=cid, jar_ref=jar["ref"], bank=jar["bank"], card=jar["card"],
                    amount=delta, balance_after=fresh.amount, currency=fresh.currency,
                    has_callback=bool(jar["callback_url"]),
                )

            await self.db.update_jar_snapshot(
                jar["ref"], last_amount=fresh.amount, last_withdrawal=fresh.withdrawal, last_error=None
            )

            if inserted:
                log.info("credit %s +%d (cur=%s) balance=%d", jar["ref"], delta, fresh.currency, fresh.amount)
                if jar["callback_url"]:
                    # негайна спроба; невдача підхопиться sweep-воркером
                    asyncio.create_task(self.sender.deliver_one(cid))
