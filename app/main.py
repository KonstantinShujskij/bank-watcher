"""Точка входу: FastAPI + фонові задачі (поллер, воркер колбеків, VPN-ротація).

Запуск:  uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import settings
from .core.callbacks import CallbackSender
from .core.poller import Poller
from .db import Database
from .vpn.nordvpn import NordVPN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("main")

WEB_STATIC = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    await db.connect()
    client = httpx.AsyncClient(timeout=settings.http_timeout)
    sender = CallbackSender(db, client)
    poller = Poller(db, client, sender)
    vpn = NordVPN()

    app.state.db = db
    app.state.client = client

    vpn.start()
    sender.start()
    poller.start()
    log.info("bank-watcher up on %s:%s", settings.host, settings.port)
    try:
        yield
    finally:
        await poller.stop()
        await sender.stop()
        await vpn.stop()
        await client.aclose()
        await db.close()


app = FastAPI(title="bank-watcher", lifespan=lifespan)
app.include_router(router)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_STATIC)), name="static")
