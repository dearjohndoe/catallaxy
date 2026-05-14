from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


# State machine:
#   pending    → refunding (atomic claim)
#   refunding  → refunded (terminal, success)
#   refunding  → pending (transient error, with backoff)
#   pending    → failed (terminal, after max attempts)
#   pending    → processed (terminal, /invoke succeeded for this tx after enqueue)
STATUS_PENDING = "pending"
STATUS_REFUNDING = "refunding"
STATUS_REFUNDED = "refunded"
STATUS_FAILED = "failed"
STATUS_PROCESSED = "processed"

TERMINAL_STATUSES = {STATUS_REFUNDED, STATUS_FAILED, STATUS_PROCESSED}


@dataclass
class PendingRefund:
    tx_hash: str
    nonce: str
    rail: str
    sender: str | None
    amount: int | None
    sku_id: str | None
    status: str
    refund_tx: str | None
    attempts: int
    last_error: str | None
    created_at: int
    last_attempt_at: int | None
    next_attempt_at: int
    # When 1, refund_worker must NOT skip on tx_store.is_processed=True.
    # Used for failures AFTER mark_processed where service was not delivered
    # (e.g., OOS race on direct refund_user fail, jobs.submit crash, runner
    # refund_user fail).
    force_refund: int = 0


class RefundQueue:
    """SQLite-backed queue of payments that couldn't be processed and need a refund.

    Used when verify_payment can't proceed (e.g. jetton_verifier unavailable
    due to LiteServer outage or misconfig). Background worker drains the queue
    by sending refunds with exponential backoff.

    Idempotency: tx_hash is PRIMARY KEY, atomic UPDATE-WHERE-status used to
    claim entries (one worker = one refund).
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=15000")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_refunds (
                tx_hash TEXT PRIMARY KEY,
                nonce TEXT NOT NULL,
                rail TEXT NOT NULL,
                sender TEXT,
                amount INTEGER,
                sku_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                refund_tx TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                last_attempt_at INTEGER,
                next_attempt_at INTEGER NOT NULL,
                force_refund INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Idempotent migration for DBs created before force_refund existed.
        try:
            await self._conn.execute(
                "ALTER TABLE pending_refunds ADD COLUMN force_refund INTEGER NOT NULL DEFAULT 0"
            )
        except aiosqlite.OperationalError:
            pass  # column already exists
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_refunds_status_next "
            "ON pending_refunds(status, next_attempt_at)"
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def enqueue(
        self,
        tx_hash: str,
        nonce: str,
        rail: str,
        sender: str | None = None,
        amount: int | None = None,
        sku_id: str | None = None,
        force_refund: bool = False,
    ) -> bool:
        """Insert pending refund. Returns True if newly inserted, False if already present.

        ``force_refund=True`` tells the worker to bypass its is_processed
        race-guard. Use only when service was NOT delivered despite
        mark_processed having succeeded (OOS race after direct refund fail,
        jobs.submit crash, runner refund fail).
        """
        if not self._conn:
            await self.init()
        now = int(time.time())
        try:
            await self._conn.execute(
                """
                INSERT INTO pending_refunds
                  (tx_hash, nonce, rail, sender, amount, sku_id, status,
                   attempts, created_at, next_attempt_at, force_refund)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (tx_hash, nonce, rail, sender, amount, sku_id, now, now,
                 1 if force_refund else 0),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get(self, tx_hash: str) -> PendingRefund | None:
        if not self._conn:
            await self.init()
        async with self._conn.execute(
            """
            SELECT tx_hash, nonce, rail, sender, amount, sku_id, status,
                   refund_tx, attempts, last_error, created_at,
                   last_attempt_at, next_attempt_at, force_refund
              FROM pending_refunds WHERE tx_hash = ?
            """,
            (tx_hash,),
        ) as cur:
            row = await cur.fetchone()
        return PendingRefund(*row) if row else None

    async def fetch_due(self, limit: int = 10) -> list[PendingRefund]:
        if not self._conn:
            await self.init()
        now = int(time.time())
        async with self._conn.execute(
            """
            SELECT tx_hash, nonce, rail, sender, amount, sku_id, status,
                   refund_tx, attempts, last_error, created_at,
                   last_attempt_at, next_attempt_at, force_refund
              FROM pending_refunds
             WHERE status = 'pending'
               AND next_attempt_at <= ?
             ORDER BY next_attempt_at ASC
             LIMIT ?
            """,
            (now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [PendingRefund(*row) for row in rows]

    async def claim(self, tx_hash: str) -> bool:
        """Atomically transition pending → refunding. Increments attempts.

        Returns True if claimed (caller must perform the refund), False if
        another worker beat us to it or the entry moved to a terminal state.
        """
        if not self._conn:
            await self.init()
        now = int(time.time())
        cur = await self._conn.execute(
            """
            UPDATE pending_refunds
               SET status = 'refunding',
                   last_attempt_at = ?,
                   attempts = attempts + 1
             WHERE tx_hash = ? AND status = 'pending'
            """,
            (now, tx_hash),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def mark_refunded(self, tx_hash: str, refund_tx: str) -> None:
        if not self._conn:
            await self.init()
        await self._conn.execute(
            "UPDATE pending_refunds SET status='refunded', refund_tx=?, last_error=NULL "
            "WHERE tx_hash=?",
            (refund_tx, tx_hash),
        )
        await self._conn.commit()

    async def mark_processed(self, tx_hash: str) -> None:
        """Mark entry as processed by /invoke (no refund needed).

        Called when a successful verify+process happens for a tx that was
        previously enqueued. Atomic on status to avoid racing the worker
        — only succeeds if status is still 'pending'.
        """
        if not self._conn:
            await self.init()
        await self._conn.execute(
            "UPDATE pending_refunds SET status='processed' "
            "WHERE tx_hash=? AND status='pending'",
            (tx_hash,),
        )
        await self._conn.commit()

    async def mark_failed_transient(
        self, tx_hash: str, error: str, backoff_seconds: int,
    ) -> None:
        """Move refunding → pending, schedule next attempt."""
        if not self._conn:
            await self.init()
        now = int(time.time())
        await self._conn.execute(
            """
            UPDATE pending_refunds
               SET status = 'pending',
                   last_error = ?,
                   next_attempt_at = ?
             WHERE tx_hash = ? AND status = 'refunding'
            """,
            (error[:500], now + backoff_seconds, tx_hash),
        )
        await self._conn.commit()

    async def mark_failed_permanent(self, tx_hash: str, error: str) -> None:
        if not self._conn:
            await self.init()
        await self._conn.execute(
            "UPDATE pending_refunds SET status='failed', last_error=? WHERE tx_hash=?",
            (error[:500], tx_hash),
        )
        await self._conn.commit()

    async def update_payment_info(
        self, tx_hash: str, sender: str, amount: int,
    ) -> None:
        """Persist sender/amount once recovered from on-chain lookup."""
        if not self._conn:
            await self.init()
        await self._conn.execute(
            "UPDATE pending_refunds SET sender=?, amount=? WHERE tx_hash=?",
            (sender, amount, tx_hash),
        )
        await self._conn.commit()

    async def list_stale_refunding(
        self, older_than_seconds: int = 600,
    ) -> list[PendingRefund]:
        """Return entries stuck in 'refunding' since before ``older_than_seconds``.

        Caller (refund_worker) probes the chain for a matching refund tx and
        decides whether to mark_refunded (already sent) or mark_failed_transient
        (revert to pending for retry). Blind reverting could cause double-refunds.
        """
        if not self._conn:
            await self.init()
        cutoff = int(time.time()) - older_than_seconds
        async with self._conn.execute(
            """
            SELECT tx_hash, nonce, rail, sender, amount, sku_id, status,
                   refund_tx, attempts, last_error, created_at,
                   last_attempt_at, next_attempt_at, force_refund
              FROM pending_refunds
             WHERE status = 'refunding'
               AND last_attempt_at IS NOT NULL
               AND last_attempt_at < ?
            """,
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return [PendingRefund(*row) for row in rows]
