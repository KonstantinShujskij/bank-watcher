"""Адаптер Privat24-зборів (next.privat24.ua/send/<code>) через headless-браузер.

ЧОМУ БРАУЗЕР (а не httpx, як mono/PUMB):
  Зібрана/цільова сума публічна (видно анонімно), але публічне API
  (`/api/p24/pub/envelopes/pubinfo`) за ГОСТЬОВОЮ сесією з `/api/p24/init`, а
  init антибот-гейтнутий: bare-запит дає 403 і з сервера, і з резидентного IP
  (fingerprint via fingerprint.pb.ua). Сесія народжується ЛИШЕ у справжньому
  браузері → читаємо headless Chromium (Playwright).

ОПТИМІЗАЦІЯ під ~5 банок одночасно:
  • ОДИН спільний браузер + контекст — гостьова сесія/fingerprint піднімається
    РАЗ і переюзається на всі банки й цикли (головна економія).
  • Ефемерна сторінка на кожен fetch (renderer звільняється одразу) +
    bounded-concurrency (семафор PRIVAT_CONCURRENCY) — 5 банок не відкривають
    5 рендерерів водночас і не з'їдають RAM.
  • Блокування зайвих запитів (картинки/шрифти/аналітика; fingerprint/payhub
    НЕ блокуємо — потрібні для сесії).
  • Recycle браузера кожні N навігацій / після серії помилок — проти лік-кріпу
    і для оновлення сесії.
  • Lazy-init: поки нема Privat-банок — браузер не стартує (нуль накладних на
    mono/PUMB). Трафік Chromium іде через VPN-тунель боксу (порти 22/8080 в
    allowlist — bypass; решта через exit).
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx  # сигнатура fetch_jar спільна; для privat httpx-клієнт НЕ використовується

from ..config import settings
from .base import BankAdapter, NormalizedJar

log = logging.getLogger("privat")

# "0.00 UAH / 25 000.00" → (зібрано, ціль) у гривнях (  = nbsp між розрядами)
_AMOUNT_RE = re.compile(r"([\d\s .,]+)\s*UAH\s*/\s*([\d\s .,]+)")
_NAME_RE = re.compile(r"([^\n]+)\n[\d\s .,]+\s*UAH\s*/")
_CODE_RE = re.compile(r"/send/([A-Za-z0-9]+)")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# суто сторонні трекери/реклама — НЕ privat/payhub/fingerprint (ті потрібні для сесії)
_BLOCK_HOSTS = ("googletagmanager", "google-analytics", "analytics.google", "doubleclick",
                "tiktok", "licdn", "mgid.com", "gstatic", "pay.google", "snap.licdn")
_LAUNCH_ARGS = ["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
                "--disable-extensions", "--disable-software-rasterizer",
                "--blink-settings=imagesEnabled=false", "--disable-background-networking",
                "--mute-audio"]


def _to_kop(uah: str) -> int:
    return round(float(uah.replace(" ", "").replace(" ", "").replace(",", ".")) * 100)


class _SharedBrowser:
    """Один headless-браузер + контекст (одна гостьова сесія) на весь процес."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self._lock = asyncio.Lock()          # серіалізує init/recycle
        self._n = max(1, settings.privat_concurrency)
        self._sem = asyncio.Semaphore(self._n)
        self._navs = 0
        self._errors = 0

    async def _ensure(self) -> None:
        if self._ctx is not None:
            return
        async with self._lock:
            if self._ctx is not None:
                return
            from playwright.async_api import async_playwright  # lazy: import лише коли є Privat-банка
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            self._ctx = await self._browser.new_context(user_agent=_UA, locale="uk-UA")
            await self._ctx.route("**/*", self._route)
            self._navs = 0
            self._errors = 0
            log.info("[privat] browser launched (concurrency=%d)", self._n)

    async def _route(self, route) -> None:
        req = route.request
        block = req.resource_type in ("image", "font", "media") or any(h in req.url for h in _BLOCK_HOSTS)
        try:
            await (route.abort() if block else route.continue_())
        except Exception:
            pass

    async def fetch(self, url: str) -> tuple[int, int, str | None]:
        """Навігація → (collected_kop, goal_kop, name). Bounded-concurrency + recycle."""
        text = None
        err: Exception | None = None
        async with self._sem:
            await self._ensure()
            page = await self._ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=settings.privat_nav_timeout_ms)
                await page.wait_for_function(
                    "() => /UAH\\s*\\//.test(document.body.innerText)",
                    timeout=settings.privat_nav_timeout_ms,
                )
                text = await page.inner_text("body")
            except Exception as e:
                err = e
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
            self._navs += 1
            self._errors = self._errors + 1 if err is not None else 0

        if self._navs >= settings.privat_recycle_every or self._errors >= settings.privat_recycle_on_errors:
            await self._recycle()
        if err is not None:
            raise err

        m = _AMOUNT_RE.search(text)
        if not m:
            raise RuntimeError("Privat: суму збору не знайдено на сторінці")
        nm = _NAME_RE.search(text)
        return _to_kop(m.group(1)), _to_kop(m.group(2)), (nm.group(1).strip() if nm else None)

    async def _recycle(self) -> None:
        # дренуємо ВСІ слоти → чекаємо завершення активних fetch, тоді закриваємо
        for _ in range(self._n):
            await self._sem.acquire()
        try:
            log.info("[privat] recycling browser (navs=%d errors=%d)", self._navs, self._errors)
            await self._close_inner()
        finally:
            for _ in range(self._n):
                self._sem.release()

    async def _close_inner(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    await getattr(obj, meth)()
            except Exception:
                pass
        self._ctx = self._browser = self._pw = None
        self._navs = self._errors = 0

    async def close(self) -> None:
        async with self._lock:
            await self._close_inner()


_BROWSER = _SharedBrowser()


class PrivatAdapter(BankAdapter):
    bank = "privat"
    poll_interval = settings.privat_poll_interval  # Privat-агрегат повільний + браузер дорогий

    def parse_ref(self, text: str) -> str:
        text = (text or "").strip()
        m = _CODE_RE.search(text)
        if m:
            return m.group(1)
        return text.split("?")[0].rstrip("/").split("/")[-1].strip()  # голий код

    async def fetch_jar(self, ref: str, client: httpx.AsyncClient) -> NormalizedJar:
        # client (httpx) НЕ використовується — Privat читається браузером.
        url = f"https://next.privat24.ua/send/{ref}"
        collected, _goal, name = await _BROWSER.fetch(url)
        # amount = зібрано (копійки), монотонність не гарантована → дедуп на стороні
        # поллера через cumulative_in (bank-agnostic).
        return NormalizedJar(ref=ref, name=name or ref, amount=collected, withdrawal=0, currency="980")


async def aclose_browser() -> None:
    """Закрити спільний браузер (виклик при shutdown сервісу)."""
    await _BROWSER.close()
