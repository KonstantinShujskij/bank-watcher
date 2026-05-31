"""Шар доступу до SQLite (aiosqlite).

Один спільний конект на процес: aiosqlite серіалізує операції у власному
воркер-потоці, тож конкурентні await з поллера / воркера колбеків / API безпечні.
"""
from __future__ import annotations

import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS jars (
    ref             TEXT PRIMARY KEY,
    bank            TEXT NOT NULL,
    url             TEXT,
    card            TEXT,
    name            TEXT,
    currency        TEXT NOT NULL DEFAULT '980',
    baseline_amount INTEGER NOT NULL DEFAULT 0,
    last_amount     INTEGER NOT NULL DEFAULT 0,
    last_withdrawal INTEGER NOT NULL DEFAULT 0,
    cumulative_in   INTEGER NOT NULL DEFAULT 0,
    callback_url    TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      INTEGER NOT NULL,
    last_polled_at  INTEGER,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS credits (
    id                  TEXT PRIMARY KEY,
    jar_ref             TEXT NOT NULL,
    bank                TEXT NOT NULL,
    card                TEXT,
    amount              INTEGER NOT NULL,
    balance_after       INTEGER NOT NULL,
    currency            TEXT NOT NULL,
    detected_at         INTEGER NOT NULL,
    callback_status     TEXT NOT NULL DEFAULT 'pending',
    callback_attempts   INTEGER NOT NULL DEFAULT 0,
    callback_last_at    INTEGER,
    callback_last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_credits_jar ON credits(jar_ref, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_credits_cb  ON credits(callback_status);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        # Міграція для наявних БД: CREATE TABLE IF NOT EXISTS не додає нову колонку
        # до вже створеної таблиці. cumulative_in — монотонний якір дедупу кредитів
        # (див. poller.credit_id), щоб повторне наповнення банки до того самого
        # балансу не відкидалось як дублікат.
        cur = await self._db.execute("PRAGMA table_info(jars)")
        cols = [r[1] for r in await cur.fetchall()]
        if "cumulative_in" not in cols:
            await self._db.execute("ALTER TABLE jars ADD COLUMN cumulative_in INTEGER NOT NULL DEFAULT 0")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    # ---------------- jars ----------------
    async def upsert_jar(self, *, ref, bank, url, card, name, currency, baseline_amount, callback_url) -> None:
        await self.db.execute(
            """
            INSERT INTO jars (ref, bank, url, card, name, currency, baseline_amount,
                              last_amount, last_withdrawal, callback_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'active', ?)
            ON CONFLICT(ref) DO UPDATE SET
                bank=excluded.bank, url=excluded.url, card=excluded.card,
                name=excluded.name, currency=excluded.currency,
                callback_url=excluded.callback_url, status='active'
            """,
            (ref, bank, url, card, name, currency, baseline_amount, baseline_amount, callback_url, now_ms()),
        )
        await self.db.commit()

    async def get_jar(self, ref):
        cur = await self.db.execute("SELECT * FROM jars WHERE ref=?", (ref,))
        return await cur.fetchone()

    async def list_jars(self):
        cur = await self.db.execute("SELECT * FROM jars ORDER BY created_at DESC")
        return await cur.fetchall()

    async def list_active_jars(self):
        cur = await self.db.execute("SELECT * FROM jars WHERE status='active'")
        return await cur.fetchall()

    async def delete_jar(self, ref) -> None:
        await self.db.execute("DELETE FROM jars WHERE ref=?", (ref,))
        await self.db.commit()

    async def set_jar_status(self, ref, status) -> None:
        await self.db.execute("UPDATE jars SET status=? WHERE ref=?", (status, ref))
        await self.db.commit()

    async def update_jar_snapshot(self, ref, *, last_amount, last_withdrawal, cumulative_in, last_error=None) -> None:
        await self.db.execute(
            "UPDATE jars SET last_amount=?, last_withdrawal=?, cumulative_in=?, last_polled_at=?, last_error=? WHERE ref=?",
            (last_amount, last_withdrawal, cumulative_in, now_ms(), last_error, ref),
        )
        await self.db.commit()

    async def mark_jar_error(self, ref, err) -> None:
        await self.db.execute(
            "UPDATE jars SET last_polled_at=?, last_error=? WHERE ref=?",
            (now_ms(), err, ref),
        )
        await self.db.commit()

    # ---------------- credits ----------------
    async def insert_credit(self, *, id, jar_ref, bank, card, amount, balance_after, currency, has_callback) -> bool:
        """Вставляє кредит. Повертає True, якщо це новий запис (idempotent по id)."""
        status = "pending" if has_callback else "skipped"
        try:
            await self.db.execute(
                """
                INSERT INTO credits (id, jar_ref, bank, card, amount, balance_after,
                                     currency, detected_at, callback_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (id, jar_ref, bank, card, amount, balance_after, currency, now_ms(), status),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_credit(self, credit_id):
        cur = await self.db.execute("SELECT * FROM credits WHERE id=?", (credit_id,))
        return await cur.fetchone()

    async def list_credits(self, jar_ref, limit=200):
        cur = await self.db.execute(
            "SELECT * FROM credits WHERE jar_ref=? ORDER BY detected_at DESC LIMIT ?",
            (jar_ref, limit),
        )
        return await cur.fetchall()

    async def sum_credits(self, jar_ref, since=0) -> int:
        """Сума всіх зарахувань (приростів) збору з моменту підписки, копійки.

        Кожен кредит — це додатний приріст балансу (поповнення), тож сума
        монотонна і НЕ зменшується, коли власник знімає кошти з банки (на
        відміну від поточного балансу `last_amount - last_withdrawal`).
        `since` за замовч. = created_at банки, щоб не зачепити можливі
        кредити від попередньої підписки того ж ref (видалення не каскадить).
        """
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM credits WHERE jar_ref=? AND detected_at >= ?",
            (jar_ref, since),
        )
        row = await cur.fetchone()
        return row["s"] if row else 0

    async def pending_callbacks(self, max_attempts, limit=50):
        cur = await self.db.execute(
            "SELECT * FROM credits WHERE callback_status='pending' AND callback_attempts < ? "
            "ORDER BY detected_at ASC LIMIT ?",
            (max_attempts, limit),
        )
        return await cur.fetchall()

    async def mark_callback_result(self, credit_id, *, success, error, max_attempts) -> None:
        cur = await self.db.execute("SELECT callback_attempts FROM credits WHERE id=?", (credit_id,))
        row = await cur.fetchone()
        attempts = (row["callback_attempts"] if row else 0) + 1
        if success:
            status = "delivered"
        elif attempts >= max_attempts:
            status = "failed"
        else:
            status = "pending"
        await self.db.execute(
            "UPDATE credits SET callback_status=?, callback_attempts=?, callback_last_at=?, "
            "callback_last_error=? WHERE id=?",
            (status, attempts, now_ms(), error, credit_id),
        )
        await self.db.commit()
