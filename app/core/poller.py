"""Поллер зборів: раз на POLL_INTERVAL опитує активні банки і фіксує зарахування.

Зарахування = приріст `amount` між опитуваннями. УВАГА: `amount` банок (моно
`jarAmount`, PUMB `total_amount`) — це ПОТОЧНИЙ баланс, він ПАДАЄ при знятті, а
не монотонне «усього зібрано» (перевірено на живому API). Тому дедуп НЕ можна
якорити на балансі: банка може наповнитись до того самого значення ще раз —
і реальне повторне зарахування відкинулось би як «дубль».

Дедуп якоримо на `cumulative_in` — нашому монотонному лічильнику суми всіх
зарахованих приростів по банці. Він рухається у `update_jar_snapshot` РАЗОМ зі
знімком (тобто ПІСЛЯ вставки кредиту), тож якщо впадемо між вставкою і знімком,
наступний тік перерахує той самий id (no-op, без дубля); а кожен РЕАЛЬНИЙ новий
депозит дає більший cumulative → унікальний id.
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


def credit_id(jar_ref: str, cumulative_in: int) -> str:
    # Якір — монотонний cumulative_in (НЕ поточний баланс, який повторюється).
    # Тег "v2" відокремлює простір id від історичних balance-based id, щоб
    # випадковий збіг числа не зіткнувся з уже відправленим раніше кредитом.
    return hashlib.sha256(f"{jar_ref}:v2:{cumulative_in}".encode()).hexdigest()[:32]


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
        # Браузер Privat лишаємо ТЕПЛИМ між підписками (варіант A): idle-teardown
        # прибрано. Холодний бутстрап (launch + SPA-навігація + xref, до ~3 хв із
        # ретраями по nav-timeout) затримував baseline нової privat-банки → транзакції
        # за цей час вшивались у baseline і губились. Тепер додавання privat-банки
        # застає браузер готовим. Пам'ять обмежує періодичний рециклінг у fetch
        # (privat_recycle_seconds=30хв) + teardown на зависання evaluate; на shutdown
        # браузер закриває lifespan (main.py).

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
                # Бекстоп-таймаут: завислий фетч (напр. headless-браузер Privat)
                # не сміє висіти вічно й клинити весь gather-тік (mono/pumb теж).
                fresh = await asyncio.wait_for(
                    adapter.fetch_jar(jar["ref"], self.client),
                    timeout=settings.poll_fetch_timeout,
                )
            except Exception as exc:
                await self.db.mark_jar_error(jar["ref"], str(exc))
                log.warning("fetch %s failed: %s", jar["ref"], exc)
                # VPN re-search лише для monobank: тільки його екзит чутливий до
                # 403 на датацентрові IP. Фейл PUMB/Privat (напр. флапаючий
                # headless-браузер) НЕ має смикати VPN-екзит, який потрібен mono.
                if self._on_fetch_error and jar["bank"] == "mono":
                    try: self._on_fetch_error()
                    except Exception: pass
                return

            delta = fresh.amount - jar["last_amount"]

            inserted = False
            cid = ""
            cumulative_in = jar["cumulative_in"]  # без кредиту — лишається незмінним
            if delta > 0:
                # 1) спершу фіксуємо кредит (idempotent по id), 2) потім рухаємо знімок
                # РАЗОМ із cumulative_in — якщо впадемо між кроками, наступний тік
                # перерахує той самий cumulative → той самий id → no-op (без дубля).
                cumulative_in = jar["cumulative_in"] + delta
                cid = credit_id(jar["ref"], cumulative_in)
                inserted = await self.db.insert_credit(
                    id=cid, jar_ref=jar["ref"], bank=jar["bank"], card=jar["card"],
                    amount=delta, balance_after=fresh.amount, currency=fresh.currency,
                    has_callback=bool(jar["callback_url"]),
                )

            await self.db.update_jar_snapshot(
                jar["ref"], last_amount=fresh.amount, last_withdrawal=fresh.withdrawal,
                cumulative_in=cumulative_in, last_error=None,
            )

            if inserted:
                log.info("credit %s +%d (cur=%s) balance=%d", jar["ref"], delta, fresh.currency, fresh.amount)
                if jar["callback_url"]:
                    # негайна спроба; невдача підхопиться sweep-воркером
                    asyncio.create_task(self.sender.deliver_one(cid))
