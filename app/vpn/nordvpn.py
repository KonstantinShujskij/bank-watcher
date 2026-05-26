"""Тонка обгортка над NordVPN CLI з періодичною ротацією вихідного IP.

Навіщо: публічне API банок лімітує per-IP; при опитуванні раз на секунду
ротація IP знижує ризик тротлінгу/бану.

ВАЖЛИВО (split-tunnel): ротація на мить рве з'єднання. Полл-цикл це переживає
(є retry на рівні тіку). Але щоб ротація НЕ рвала вхідні з'єднання до нашого
API/фронта, на сервері треба винести наш порт з тунелю, напр.:

    nordvpn allowlist add port <PORT>

Локально лишай VPN_ENABLED=false.
"""
from __future__ import annotations

import asyncio
import itertools
import logging

from ..config import settings

log = logging.getLogger("vpn")


class NordVPN:
    def __init__(self) -> None:
        self._countries = itertools.cycle(settings.vpn_countries or ["Ukraine"])
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not settings.vpn_enabled:
            log.info("vpn disabled (VPN_ENABLED=false)")
            return
        self._task = asyncio.create_task(self._run(), name="vpn")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        await self._connect(next(self._countries))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.vpn_rotate_seconds)
            except asyncio.TimeoutError:
                await self._connect(next(self._countries))

    async def _connect(self, country: str) -> None:
        rc, out = await self._run_cli("connect", country)
        if rc == 0:
            log.info("vpn connected: %s", country)
        else:
            log.warning("vpn connect %s failed (rc=%s): %s", country, rc, out)

    async def status(self) -> str:
        _, out = await self._run_cli("status")
        return out

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
