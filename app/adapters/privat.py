"""Адаптер Privat24-зборів (next.privat24.ua/send/<code>) через headless-браузер.

ЧОМУ БРАУЗЕР (а не httpx, як mono/PUMB):
  Сума збору публічна, але публічне API (`/api/p24/pub/...`) за ГОСТЬОВОЮ сесією
  з `/api/p24/init`, який антибот-гейтнутий (fingerprint) → bare httpx-init дає
  403. Сесія народжується ЛИШЕ у справжньому браузері.

ШВИДКА МОДЕЛЬ (in-page pubinfo замість повного рендеру):
  Один headless-браузер + контекст + ОДНА персистентна сторінка тримають
  гостьову сесію. `xref` — СЕСІЙНИЙ токен (один на сесію), тож ОДНА сесія полить
  УСІ банки легкими XHR-ами прямо зі сторінки:
    • ziplink {action:"get", hash:<code>, type:"sharing", xref}  → data.value → refEnv
    • envelopes/pubinfo {xref, refEnv}                            → data.deposit (зібрано)
  refEnv кешуємо на code. Полл = ~частки секунди (без рендеру) → пам'ять стабільна
  (~0.6 ГБ) незалежно від кількості банок і частоти. Сесію бутстрапимо ОДНОЮ
  повною навігацією (звідти ловимо xref). При протуханні (pubinfo != success) —
  re-bootstrap; повна навігація з читанням суми з DOM лишається фолбеком.

  Lazy-init: поки нема Privat-банок — браузер не стартує. Трафік іде через
  VPN-тунель боксу (порти 22/8080 в allowlist — bypass).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx  # сигнатура fetch_jar спільна; httpx-клієнт для privat НЕ використовується

from ..config import settings
from .base import BankAdapter, NormalizedJar

log = logging.getLogger("privat")

_AMOUNT_RE = re.compile(r"([\d\s .,]+)\s*UAH\s*/\s*([\d\s .,]+)")  # фолбек-парс DOM
_CODE_RE = re.compile(r"/send/([A-Za-z0-9]+)")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124 Safari/537.36")
_BLOCK_HOSTS = ("googletagmanager", "google-analytics", "analytics.google", "doubleclick",
                "tiktok", "licdn", "mgid.com", "gstatic", "pay.google", "snap.licdn")
_LAUNCH_ARGS = ["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
                "--disable-extensions", "--disable-software-rasterizer",
                "--blink-settings=imagesEnabled=false", "--disable-background-networking",
                "--mute-audio"]

# In-page: ziplink(code)->refEnv (якщо треба) + pubinfo(refEnv)->дані. Date.now() — у браузері.
_PUBINFO_JS = """
async ({hash, refEnv, xref}) => {
  const base = "/api/p24/pub";
  const post = async (p, b) => {
    const r = await fetch(base + p, {method: "POST", credentials: "include",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({}, b, {xref, _: Date.now()}))});
    return await r.json().catch(() => null);
  };
  let env = refEnv;
  if (!env) {
    const z = await post("/ziplink", {action: "get", hash, type: "sharing"});
    if (!z || z.status !== "success") return {ok: false, step: "ziplink", status: z && z.status, msg: z && z.message};
    try { env = JSON.parse(z.data.value).payload.refEnv; } catch (e) { return {ok: false, step: "ziplink-parse"}; }
  }
  const pi = await post("/envelopes/pubinfo", {refEnv: env});
  if (!pi || pi.status !== "success") return {ok: false, step: "pubinfo", status: pi && pi.status, msg: pi && pi.message, refEnv: env};
  const d = pi.data || {};
  return {ok: true, refEnv: env, deposit: d.deposit, available: d.availableBalance, goal: d.goalAmount, name: d.envName};
}
"""


def _to_kop(v) -> int:
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    return round(float(s) * 100)


def _code(url_or_code: str) -> str:
    s = (url_or_code or "").strip()
    m = _CODE_RE.search(s)
    return m.group(1) if m else s.split("?")[0].rstrip("/").split("/")[-1].strip()


class _PrivatSession:
    """Одна гостьова сесія (браузер+контекст+сторінка) на всі банки."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._xref: str | None = None
        self._refenv: dict[str, str] = {}   # code -> refEnv (кеш)
        self._lock = asyncio.Lock()          # серіалізує доступ до єдиної сторінки
        self._polls = 0

    async def _route(self, route) -> None:
        req = route.request
        block = req.resource_type in ("image", "font", "media") or any(h in req.url for h in _BLOCK_HOSTS)
        try:
            await (route.abort() if block else route.continue_())
        except Exception:
            pass

    async def _bootstrap(self, code: str) -> None:
        """Підняти браузер (за потреби) і навігацією встановити сесію + зловити xref."""
        if self._page is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            self._ctx = await self._browser.new_context(user_agent=_UA, locale="uk-UA")
            await self._ctx.route("**/*", self._route)
            self._page = await self._ctx.new_page()
            log.info("[privat] browser launched")

        xref_box: dict[str, str] = {}

        def on_req(req):
            if "xref" in xref_box:
                return
            if "ziplink" in req.url or "pubinfo" in req.url:
                try:
                    x = json.loads(req.post_data or "{}").get("xref")
                    if x:
                        xref_box["xref"] = x
                except Exception:
                    pass

        self._page.on("request", on_req)
        try:
            await self._page.goto(f"https://next.privat24.ua/send/{code}",
                                  wait_until="domcontentloaded", timeout=settings.privat_nav_timeout_ms)
            # дочекатись, поки SPA зробить ziplink/pubinfo (звідти xref) — індикатор: сума в DOM
            try:
                await self._page.wait_for_function(
                    "() => /UAH\\s*\\//.test(document.body.innerText)", timeout=settings.privat_nav_timeout_ms)
            except Exception:
                pass
        finally:
            self._page.remove_listener("request", on_req)

        if xref_box.get("xref"):
            self._xref = xref_box["xref"]
            log.info("[privat] session established (xref captured)")
        elif not self._xref:
            # не зловили xref і нема старого → фолбек-значення витягнемо з DOM у виклику
            raise RuntimeError("Privat: не вдалося захопити xref сесії")

    async def _dom_amount(self) -> int | None:
        """Фолбек: прочитати зібране з відрендереної сторінки (після _bootstrap)."""
        try:
            m = _AMOUNT_RE.search(await self._page.inner_text("body"))
            return _to_kop(m.group(1)) if m else None
        except Exception:
            return None

    async def fetch(self, code: str) -> tuple[int, str | None]:
        """(collected_kop, name). Швидкий шлях pubinfo; re-bootstrap при протуханні."""
        async with self._lock:
            for attempt in (1, 2):
                if self._page is None or self._xref is None:
                    await self._bootstrap(code)
                res = await self._page.evaluate(
                    _PUBINFO_JS, {"hash": code, "refEnv": self._refenv.get(code), "xref": self._xref})
                if res and res.get("ok"):
                    self._refenv[code] = res["refEnv"]
                    self._polls += 1
                    amount = res.get("deposit")
                    if amount is None:
                        amount = res.get("available")
                    return _to_kop(amount), res.get("name")
                # невдача → сесія/xref протухли: скинути й re-bootstrap (1 повтор)
                log.warning("[privat] api fetch failed (%s) for %s — re-bootstrap", res, code)
                self._xref = None
                self._refenv.pop(code, None)
                if attempt == 1:
                    await self._bootstrap(code)
                    continue
                # другий провал — фолбек на DOM відрендереної сторінки
                dom = await self._dom_amount()
                if dom is not None:
                    return dom, None
                raise RuntimeError(f"Privat: не вдалося отримати дані ({res})")

    async def close(self) -> None:
        async with self._lock:
            for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
                try:
                    if obj:
                        await getattr(obj, meth)()
                except Exception:
                    pass
            self._pw = self._browser = self._ctx = self._page = None
            self._xref = None
            self._refenv.clear()


_SESSION = _PrivatSession()


class PrivatAdapter(BankAdapter):
    bank = "privat"
    poll_interval = settings.privat_poll_interval

    def parse_ref(self, text: str) -> str:
        return _code(text)

    async def fetch_jar(self, ref: str, client: httpx.AsyncClient) -> NormalizedJar:
        # client (httpx) НЕ використовується — Privat читається браузером.
        collected, name = await _SESSION.fetch(ref)
        # amount = зібрано (копійки); дедуп на стороні поллера через cumulative_in.
        return NormalizedJar(ref=ref, name=name or ref, amount=collected, withdrawal=0, currency="980")


async def aclose_browser() -> None:
    await _SESSION.close()
