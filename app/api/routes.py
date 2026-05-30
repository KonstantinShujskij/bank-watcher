"""HTTP API: реєстрація зборів, перегляд історії зарахувань, health.

Спільні залежності (БД, httpx-клієнт) беремо з app.state через Depends.
Підпис вхідних запитів від ncP2P (HMAC) додамо, коли узгоджуватимемо комунікацію.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..adapters import available_banks, detect_bank, get_adapter
from ..db import Database
from ..models import CreditOut, JarOut, RegisterJarRequest, ResolveRefOut, ResolveRefRequest

router = APIRouter(prefix="/api")


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.client


async def _jar_out(row, db: Database) -> JarOut:
    return JarOut(
        ref=row["ref"], bank=row["bank"], url=row["url"], card=row["card"],
        name=row["name"], currency=row["currency"],
        baseline_amount=row["baseline_amount"], last_amount=row["last_amount"],
        last_withdrawal=row["last_withdrawal"],
        balance=row["last_amount"] - row["last_withdrawal"],
        accumulated=await db.sum_credits(row["ref"], since=row["created_at"]),
        callback_url=row["callback_url"], status=row["status"],
        created_at=row["created_at"], last_polled_at=row["last_polled_at"],
        last_error=row["last_error"],
    )


def _credit_out(row) -> CreditOut:
    return CreditOut(
        id=row["id"], jar_ref=row["jar_ref"], bank=row["bank"], card=row["card"],
        amount=row["amount"], balance_after=row["balance_after"], currency=row["currency"],
        detected_at=row["detected_at"], callback_status=row["callback_status"],
        callback_attempts=row["callback_attempts"], callback_last_at=row["callback_last_at"],
        callback_last_error=row["callback_last_error"],
    )


@router.get("/health")
async def health(db: Database = Depends(get_db)):
    return {"status": "ok", "active_jars": len(await db.list_active_jars()), "banks": available_banks()}


@router.get("/banks")
async def banks():
    return {"banks": available_banks()}


@router.post("/jars", response_model=JarOut)
async def register_jar(
    req: RegisterJarRequest,
    db: Database = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_client),
):
    bank = req.bank or detect_bank(req.url) or "mono"
    try:
        adapter = get_adapter(bank)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        ref = await adapter.resolve_ref(req.url, client)
    except Exception as e:
        raise HTTPException(400, f"Не вдалося розпізнати ref збору: {e}")
    if not ref:
        raise HTTPException(400, "Не вдалося розпізнати ref збору з url")
    try:
        jar = await adapter.fetch_jar(ref, client)
    except Exception as e:
        raise HTTPException(502, f"Не вдалося отримати дані збору: {e}")
    # baseline = поточний баланс на момент підписки → не фаєримо вже зібране
    await db.upsert_jar(
        ref=ref, bank=bank, url=req.url, card=req.card, name=jar.name,
        currency=jar.currency, baseline_amount=jar.amount, callback_url=req.callback_url,
    )
    return await _jar_out(await db.get_jar(ref), db)


@router.post("/jars/resolve", response_model=ResolveRefOut)
async def resolve_ref(
    req: ResolveRefRequest,
    client: httpx.AsyncClient = Depends(get_client),
):
    """Розпарсити ref банки з url БЕЗ підписки/запису в БД.

    ncP2P викликає це на pre-check у діалозі створення виводу, щоб дізнатися
    ref і показати, чи ця банка вже використовувалась іншими виводами. Парсинг
    лишається єдиним джерелом істини тут (а не дублюється в ncP2P).
    """
    bank = req.bank or detect_bank(req.url) or "mono"
    try:
        adapter = get_adapter(bank)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        ref = await adapter.resolve_ref(req.url, client)
    except Exception as e:
        raise HTTPException(400, f"Не вдалося розпізнати ref збору: {e}")
    if not ref:
        raise HTTPException(400, "Не вдалося розпізнати ref збору з url")
    return ResolveRefOut(ref=ref, bank=bank)


@router.get("/jars", response_model=list[JarOut])
async def list_jars(db: Database = Depends(get_db)):
    return [await _jar_out(r, db) for r in await db.list_jars()]


@router.get("/jars/{ref}", response_model=JarOut)
async def get_jar(ref: str, db: Database = Depends(get_db)):
    row = await db.get_jar(ref)
    if not row:
        raise HTTPException(404, "Банку не знайдено")
    return await _jar_out(row, db)


@router.delete("/jars/{ref}")
async def delete_jar(ref: str, db: Database = Depends(get_db)):
    if not await db.get_jar(ref):
        raise HTTPException(404, "Банку не знайдено")
    await db.delete_jar(ref)
    return {"deleted": ref}


@router.post("/jars/{ref}/pause", response_model=JarOut)
async def pause_jar(ref: str, db: Database = Depends(get_db)):
    if not await db.get_jar(ref):
        raise HTTPException(404, "Банку не знайдено")
    await db.set_jar_status(ref, "paused")
    return await _jar_out(await db.get_jar(ref), db)


@router.post("/jars/{ref}/resume", response_model=JarOut)
async def resume_jar(ref: str, db: Database = Depends(get_db)):
    if not await db.get_jar(ref):
        raise HTTPException(404, "Банку не знайдено")
    await db.set_jar_status(ref, "active")
    return await _jar_out(await db.get_jar(ref), db)


@router.get("/jars/{ref}/credits", response_model=list[CreditOut])
async def jar_credits(ref: str, limit: int = 200, db: Database = Depends(get_db)):
    if not await db.get_jar(ref):
        raise HTTPException(404, "Банку не знайдено")
    return [_credit_out(r) for r in await db.list_credits(ref, limit=limit)]
