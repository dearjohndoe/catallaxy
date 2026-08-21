from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


INTENT_ACCEPTED = "accepted"
INTENT_FULFILLED = "fulfilled"
INTENT_REFUNDED = "refunded"

_INTENT_TERMINAL = {INTENT_FULFILLED, INTENT_REFUNDED}


@dataclass(frozen=True)
class PaymentIntentDraft:
    """Fields written with the hash row. Status is always ``accepted`` at insert."""

    identity: str
    nonce: str
    rail: str
    sender: str | None
    amount: int | None
    sku_id: str | None


@dataclass(frozen=True)
class PaymentIntent:
    tx_hash: str
    identity: str
    nonce: str
    rail: str
    sender: str | None
    amount: int | None
    sku_id: str | None
    status: str
    created_at: int
    updated_at: int


class ProcessedTxStore:
    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        # Track fire-and-forget cleanup tasks so close() can drain them
        # instead of leaving pending tasks with a dangling connection ref.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        # WAL: readers don't block writers; busy_timeout: wait up to 15s for
        # the writer lock before failing. Required because /invoke and the
        # 30-day cleanup task both write here, and refund_worker reads.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=15000")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_txs (
                tx_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        # Keys are chain-namespaced "{chain}:{tx_hash}". Prefix legacy bare keys
        # (no ':', all TON) with 'ton:'. Idempotent — skips namespaced rows.
        await self._conn.execute(
            "UPDATE processed_txs SET tx_hash = 'ton:' || tx_hash "
            "WHERE tx_hash NOT LIKE '%:%'"
        )
        # Crash-recovery ledger. Same file, same connection as processed_txs
        # so mark_processed can INSERT hash + intent in one commit. PK is the
        # namespaced on-chain hash (exactly-once unit). identity is the
        # refund-queue key. New table: no backfill of historical payments.
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_intents (
                tx_hash TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                nonce TEXT NOT NULL,
                rail TEXT NOT NULL,
                sender TEXT,
                amount INTEGER,
                sku_id TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_intents_status_created "
            "ON payment_intents(status, created_at)"
        )
        await self._conn.commit()

    async def is_processed(self, tx_hash: str) -> bool:
        if not self._conn:
            await self.init()
        async with self._conn.execute(
            "SELECT 1 FROM processed_txs WHERE tx_hash = ?",
            (tx_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def mark_processed(
        self,
        tx_hash: str,
        intent: PaymentIntentDraft | None = None,
    ) -> None:
        if not self._conn:
            await self.init()
        now_iso = datetime.now(timezone.utc).isoformat()
        now_unix = int(time.time())
        try:
            await self._conn.execute(
                "INSERT INTO processed_txs (tx_hash, created_at) VALUES (?, ?)",
                (tx_hash, now_iso),
            )
            if intent is not None:
                await self._conn.execute(
                    """
                    INSERT INTO payment_intents (
                        tx_hash, identity, nonce, rail, sender, amount, sku_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx_hash,
                        intent.identity,
                        intent.nonce,
                        intent.rail,
                        intent.sender,
                        intent.amount,
                        intent.sku_id,
                        INTENT_ACCEPTED,
                        now_unix,
                        now_unix,
                    ),
                )
            await self._conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                await self._conn.rollback()
            raise

        # Run in background. Store history for 30 days.
        task = asyncio.create_task(self.cleanup(older_than_seconds=30 * 24 * 3600))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def get_intent(self, tx_hash: str) -> PaymentIntent | None:
        if not self._conn:
            await self.init()
        async with self._conn.execute(
            """
            SELECT tx_hash, identity, nonce, rail, sender, amount, sku_id,
                   status, created_at, updated_at
              FROM payment_intents WHERE tx_hash = ?
            """,
            (tx_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return PaymentIntent(*row) if row else None

    async def set_intent_status(self, tx_hash: str, status: str) -> bool:
        """accepted → fulfilled|refunded. No-op on missing or already-terminal rows."""
        if status not in _INTENT_TERMINAL:
            raise ValueError(f"invalid intent status {status!r}")
        if not self._conn:
            await self.init()
        now_unix = int(time.time())
        cur = await self._conn.execute(
            """
            UPDATE payment_intents
               SET status = ?, updated_at = ?
             WHERE tx_hash = ? AND status = ?
            """,
            (status, now_unix, tx_hash, INTENT_ACCEPTED),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def list_stale_accepted(self, older_than_seconds: int) -> list[PaymentIntent]:
        if not self._conn:
            await self.init()
        cutoff = int(time.time()) - max(int(older_than_seconds), 0)
        async with self._conn.execute(
            """
            SELECT tx_hash, identity, nonce, rail, sender, amount, sku_id,
                   status, created_at, updated_at
              FROM payment_intents
             WHERE status = ? AND created_at <= ?
             ORDER BY created_at ASC
            """,
            (INTENT_ACCEPTED, cutoff),
        ) as cursor:
            rows = await cursor.fetchall()
        return [PaymentIntent(*row) for row in rows]

    async def close(self) -> None:
        # Drain any pending cleanup tasks first so they don't touch the
        # connection after we close it.
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._conn:
            await self._conn.close()

    async def cleanup(self, older_than_seconds: int) -> None:
        if not self._conn:
            await self.init()
        cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        cutoff_iso = cutoff_time.isoformat()
        await self._conn.execute(
            "DELETE FROM processed_txs WHERE created_at < ?",
            (cutoff_iso,),
        )
        cutoff_unix = int(cutoff_time.timestamp())
        # Drop resolved intents with the same 30-day window. Never delete
        # accepted — those are unpaid crash-recovery rows.
        await self._conn.execute(
            """
            DELETE FROM payment_intents
             WHERE status IN (?, ?) AND created_at < ?
            """,
            (INTENT_FULFILLED, INTENT_REFUNDED, cutoff_unix),
        )
        await self._conn.commit()
