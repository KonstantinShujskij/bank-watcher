"""Точка входу: FastAPI + фонові задачі (поллер, воркер колбеків, VPN-ротація).

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .adapters import get_adapter
from .adapters.privat import aclose_browser as aclose_privat_browser
from .api.routes import router
from .config import settings
from .core.callbacks import CallbackSender
from .core.memtrace import MemTracer, maybe_start as memtrace_start
from .core.poller import Poller
from .db import Database
from .vpn.nordvpn import NordVPN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("main")

# Опційний пошук витоку памʼяті: стартуємо tracemalloc якомога раніше (до
# основних алокацій). Вмикається env TRACEMALLOC=1; інакше нульовий оверхед.
memtrace_start()

WEB_STATIC = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    await db.connect()
    client = httpx.AsyncClient(timeout=settings.http_timeout)
    sender = CallbackSender(db, client)

    async def vpn_probe() -> bool:
        # окремий клієнт → свіже з'єднання через поточний тунель (без stale-pool)
        try:
            async with httpx.AsyncClient(timeout=settings.http_timeout) as c:
                await get_adapter("mono").fetch_jar(settings.vpn_probe_jar, c)
            return True
        except Exception:
            return False

    vpn = NordVPN(vpn_probe)
    # поллер сигналить VPN при помилках фетчу → реконект лише за потреби
    poller = Poller(db, client, sender, on_fetch_error=vpn.request_research)

    app.state.db = db
    app.state.client = client

    tracer = MemTracer()   # no-op, якщо tracemalloc вимкнено

    vpn.start()
    sender.start()
    poller.start()
    tracer.start()
    log.info("bank-watcher up on %s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        await tracer.stop()
        await poller.stop()
        await sender.stop()
        await vpn.stop()
        await aclose_privat_browser()   # закрити headless-браузер Privat, якщо стартував
        await client.aclose()
        await db.close()


app = FastAPI(title="bank-watcher", lifespan=lifespan)

PUBLIC_PATHS = {"/login", "/logout"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Захист усього, крім логіну. Люди — сесійна кука; ncP2P — HMAC-підпис тіла."""
    path = request.url.path
    if path in PUBLIC_PATHS or auth.validate(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    # машинні (service-to-service) виклики від ncP2P — HMAC над сирим тілом
    sig = request.headers.get("X-Signature")
    if sig and auth.verify_signature(await request.body(), sig):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(WEB_STATIC / "login.html")


@app.post("/login", include_in_schema=False)
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if not auth.verify_credentials(username, password):
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        auth.COOKIE_NAME, auth.create_session(),
        httponly=True, samesite="lax", max_age=settings.session_ttl_hours * 3600,
    )
    return resp


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    auth.destroy(request.cookies.get(auth.COOKIE_NAME))
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


app.include_router(router)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_STATIC)), name="static")
