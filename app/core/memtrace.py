"""Опційний tracemalloc-репортер для пошуку витоку памʼяті.

Вмикається env-прапором `TRACEMALLOC=1` (за замовч. ВИМКНЕНО — нульовий
оверхед). Раз на `TRACEMALLOC_INTERVAL` сек логує у WARNING (легко грепати
`[mem]`):
  - RSS процесу;
  - Python-traced памʼять (cur/peak) — ЦЕ ключ: якщо вона пласка, а RSS росте →
    витік НЕ в обʼєктах Python (фрагментація glibc / C-рівень); якщо росте →
    нижче топ-N покаже file:line, що тримає памʼять;
  - топ-N місць алокації з коротким стеком (file:line × frames).

tracemalloc треба стартувати ЯКОМОГА РАНІШЕ (до основних алокацій), тому
`maybe_start()` викликається на імпорті main.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tracemalloc

log = logging.getLogger("memtrace")


def _enabled() -> bool:
    return os.getenv("TRACEMALLOC", "").lower() in ("1", "true", "yes", "on")


def maybe_start() -> None:
    """Стартувати tracemalloc на старті процесу, якщо ввімкнено прапором."""
    if not _enabled():
        return
    frames = int(os.getenv("TRACEMALLOC_FRAMES", "4"))
    tracemalloc.start(frames)
    log.info("tracemalloc started (frames=%d)", frames)


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return -1.0


class MemTracer:
    """Фонова задача: періодично логує знімок памʼяті. No-op, якщо tracemalloc вимкнено."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.interval = float(os.getenv("TRACEMALLOC_INTERVAL", "1800"))  # 30 хв
        self.topn = int(os.getenv("TRACEMALLOC_TOP", "12"))

    def start(self) -> None:
        if not tracemalloc.is_tracing():
            return
        self._task = asyncio.create_task(self._run(), name="memtrace")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("memtrace reporter every %.0fs (top %d)", self.interval, self.topn)
        # перший звіт одразу — як база відліку
        self._report()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            self._report()

    def _report(self) -> None:
        try:
            cur, peak = tracemalloc.get_traced_memory()
            snap = tracemalloc.take_snapshot()
            stats = snap.statistics("traceback")[: self.topn]
            log.warning(
                "[mem] RSS=%.1fMB python_traced cur=%.1fMB peak=%.1fMB — top %d allocation sites:",
                _rss_mb(), cur / 1048576, peak / 1048576, self.topn,
            )
            for i, st in enumerate(stats, 1):
                log.warning("[mem] #%d  %.2fMB  %d blocks", i, st.size / 1048576, st.count)
                for line in st.traceback.format():
                    log.warning("[mem]      %s", line)
        except Exception:
            log.exception("[mem] report failed")
