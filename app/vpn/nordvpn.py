"""NordVPN CLI з пошуком РОБОЧОГО екзиту (monobank 403'ить частину VPN-IP).

Логіка:
  - на СТАРТІ: connect → проба (реальний фетч банк-API) → робочий екзит лишаємо;
    невдало — реконект на інший екзит, до vpn_max_attempts;
  - ДАЛІ: НІЯКОЇ періодичної ротації. Реконект лише РЕАКТИВНО — коли поллер
    повідомляє про помилки (через request_research()). Кулдаун анти-трешингу.
  - якщо робочого екзиту нема і vpn_fallback_direct=true — від'єднуємось
    (прямий IP сервера працює), щоб сервіс не лишився без зв'язку.

Split-tunnel: порти 22 і 8080 в allowlist (SSH/API переживають реконект).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Awaitable, Callable

import httpx

from ..config import settings

log = logging.getLogger("vpn")


class NordVPN:
    def __init__(self, probe: Callable[[], Awaitable[bool]]) -> None:
        # probe() -> True, якщо банк-API доступне з поточного екзиту
        self._probe = probe
        self._countries = itertools.cycle(settings.vpn_countries or ["Ukraine"])
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._research = asyncio.Event()
        self._last_search = 0.0

    def start(self) -> None:
        if not settings.vpn_enabled:
            log.info("vpn disabled (VPN_ENABLED=false)")
            return
        self._task = asyncio.create_task(self._run(), name="vpn")

    async def stop(self) -> None:
        self._stop.set()
        self._research.set()  # розблокувати _run
        if self._task:
            await self._task

    def request_research(self) -> None:
        """Зовнішній сигнал (від поллера): поточний екзит, схоже, не робочий."""
        self._research.set()

    async def _run(self) -> None:
        await self._ensure_working_exit()
        while not self._stop.is_set():
            await self._research.wait()
            self._research.clear()
            if self._stop.is_set():
                break
            # анти-трешинг: не шукаємо частіше, ніж раз на cooldown
            if time.monotonic() - self._last_search < settings.vpn_research_cooldown:
                continue
            log.warning("poller reported errors → re-searching vpn exit")
            await self._ensure_working_exit()

    async def _ensure_working_exit(self) -> bool:
        """Перебираємо екзити, доки проба не пройде. Інакше — fallback на прямий IP."""
        self._last_search = time.monotonic()
        for attempt in range(1, settings.vpn_max_attempts + 1):
            country = next(self._countries)
            await self._connect(country)
            await asyncio.sleep(settings.vpn_settle_seconds)
            if await self._probe_safe():
                log.info("vpn exit OK via %s (ip=%s) after %d attempt(s)",
                         country, await self._current_ip(), attempt)
                self._last_search = time.monotonic()
                return True
            log.warning("vpn exit %s blocked/failing (attempt %d/%d) → reconnecting",
                        country, attempt, settings.vpn_max_attempts)

        if settings.vpn_fallback_direct:
            log.error("no working vpn exit in %d attempts → FALLBACK to direct IP",
                      settings.vpn_max_attempts)
            await self._run_cli("disconnect")
        else:
            log.error("no working vpn exit in %d attempts → staying on last exit (degraded)",
                      settings.vpn_max_attempts)
        self._last_search = time.monotonic()
        return False

    async def _probe_safe(self) -> bool:
        try:
            return await self._probe()
        except Exception as exc:
            log.warning("vpn probe error: %s", exc)
            return False

    async def _connect(self, country: str) -> None:
        rc, out = await self._run_cli("connect", country)
        if rc != 0:
            log.warning("vpn connect %s failed (rc=%s): %s", country, rc, out)

    async def _current_ip(self) -> str:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                return (await c.get("https://api.ipify.org")).text.strip()
        except Exception:
            return "?"

    @staticmethod
    async def _run_cli(*args: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nordvpn", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            return proc.returncode or 0, stdout.decode(errors="replace").strip()
        except FileNotFoundError:
            return 127, "nordvpn CLI not found"
