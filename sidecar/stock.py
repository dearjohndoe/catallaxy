from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiosqlite

from settings import AgentSku

logger = logging.getLogger(__name__)


class StockError(Exception):
    pass


class OutOfStockError(StockError):
    pass


class UnknownSkuError(StockError):
    pass


@dataclass(frozen=True)
class StockView:
    sku_id: str
    title: str
    price_ton: int | None
    price_usd: int | None
    total: int | None          # None => infinite
    sold: int
    reserved: int
    stock_left: int | None     # None => infinite


class StockStore:
    """SQLite-backed inventory tracker with in-memory per-SKU async locks.

    - `reserve(sku_id, key, ttl)` — atomically decrement available stock and
      insert a reservation row tied to `key` (quote_id or nonce). Returns
      False if out of stock.
    - `attach_job(key, job_id)` — bind a reservation to a running job for
      later commit/release.
    - `commit_sold(key)` — finalize a sold unit: increment sold counter +
      ledger entry + drop reservation. Called after agent returns success.
    - `release(key)` — drop reservation without committing (agent failure,
      timeout, payment failed after reservation).
    - `agent_out_of_stock(key)` — agent reported stock loss: decrement total
      by 1, drop reservation, ledger entry. Refund is handled by caller.
    - `sweep_expired(now)` — delete reservations past their TTL.
    - `adjust(sku_id, delta, reason)` — CLI-driven stock mutation.
    - Reservations are cleared in-memory; total is persisted in `skus`.
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._sku_cache: dict[str, AgentSku] = {}

    async def init(self, skus: Iterable[AgentSku]) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=15000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skus (
                sku_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price_ton INTEGER,
                price_usd INTEGER,
                total INTEGER,           -- NULL => infinite
                sold INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                CHECK (price_ton IS NOT NULL OR price_usd IS NOT NULL)
            );
            CREATE TABLE IF NOT EXISTS stock_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku_id TEXT NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                job_id TEXT,
                ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stock_reservations (
                key TEXT PRIMARY KEY,
                sku_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                job_id TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS stock_reservations_sku_idx ON stock_reservations(sku_id);
            CREATE INDEX IF NOT EXISTS stock_reservations_expires_idx ON stock_reservations(expires_at);
            """
        )
        await self._conn.commit()

        now = int(time.time())
        for sku in skus:
            self._sku_cache[sku.sku_id] = sku
            self._locks.setdefault(sku.sku_id, asyncio.Lock())
            # Idempotent seed: insert row if missing; never overwrite total
            async with self._conn.execute(
                "SELECT sku_id FROM skus WHERE sku_id = ?", (sku.sku_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await self._conn.execute(
                    """
                    INSERT INTO skus (sku_id, title, price_ton, price_usd, total, sold, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (sku.sku_id, sku.title, sku.price_ton, sku.price_usd,
                     sku.initial_stock, now, now),
                )
            else:
                # Update mutable metadata (title, prices) from config on restart.
                await self._conn.execute(
                    """
                    UPDATE skus SET title = ?, price_ton = ?, price_usd = ?, updated_at = ?
                    WHERE sku_id = ?
                    """,
                    (sku.title, sku.price_ton, sku.price_usd, now, sku.sku_id),
                )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ── Queries ────────────────────────────────────────────────────

    def _sku_lock(self, sku_id: str) -> asyncio.Lock:
        lock = self._locks.get(sku_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sku_id] = lock
        return lock

    async def _require_sku(self, sku_id: str) -> AgentSku:
        spec = self._sku_cache.get(sku_id)
        if spec is None:
            raise UnknownSkuError(sku_id)
        return spec

    async def get_view(self, sku_id: str, now: int | None = None) -> StockView:
        assert self._conn is not None
        spec = await self._require_sku(sku_id)
        now = int(time.time()) if now is None else now
        async with self._conn.execute(
            "SELECT total, sold FROM skus WHERE sku_id = ?", (sku_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        total, sold = row[0], row[1]
        async with self._conn.execute(
            "SELECT COUNT(*) FROM stock_reservations WHERE sku_id = ? AND expires_at > ?",
            (sku_id, now),
        ) as cur:
            rrow = await cur.fetchone()
        reserved = int(rrow[0]) if rrow else 0
        if total is None:
            stock_left: int | None = None
        else:
            stock_left = max(total - sold - reserved, 0)
        return StockView(
            sku_id=sku_id, title=spec.title,
            price_ton=spec.price_ton, price_usd=spec.price_usd,
            total=total, sold=sold, reserved=reserved, stock_left=stock_left,
        )

    async def list_views(self) -> list[StockView]:
        return [await self.get_view(sku_id) for sku_id in self._sku_cache.keys()]

    def has_tracked_stock(self, sku_id: str) -> bool:
        spec = self._sku_cache.get(sku_id)
        return spec is not None and spec.initial_stock is not None

    # ── Reservations ───────────────────────────────────────────────

    async def reserve(self, sku_id: str, key: str, ttl_seconds: int) -> bool:
        """Reserve one unit under `key`. Returns True on success, False if sold out.

        No-op success for infinite-stock SKUs (still inserts reservation row
        so commit_sold/release accounting remains consistent, but total is
        NULL so commit_sold won't decrement anything).
        """
        assert self._conn is not None
        await self._require_sku(sku_id)
        now = int(time.time())
        expires_at = now + ttl_seconds

        async with self._sku_lock(sku_id):
            # Idempotent: if reservation for this key already exists, extend TTL.
            async with self._conn.execute(
                "SELECT sku_id FROM stock_reservations WHERE key = ?", (key,),
            ) as cur:
                existing = await cur.fetchone()
            if existing is not None:
                if existing[0] != sku_id:
                    raise StockError(f"reservation key '{key}' already bound to '{existing[0]}'")
                await self._conn.execute(
                    "UPDATE stock_reservations SET expires_at = ? WHERE key = ?",
                    (expires_at, key),
                )
                await self._conn.commit()
                return True

            # Check availability (skip for infinite-stock SKUs)
            if self.has_tracked_stock(sku_id):
                async with self._conn.execute(
                    "SELECT total, sold FROM skus WHERE sku_id = ?", (sku_id,),
                ) as cur:
                    srow = await cur.fetchone()
                assert srow is not None
                total, sold = srow[0], srow[1]
                async with self._conn.execute(
                    "SELECT COUNT(*) FROM stock_reservations WHERE sku_id = ? AND expires_at > ?",
                    (sku_id, now),
                ) as cur:
                    rrow = await cur.fetchone()
                reserved = int(rrow[0]) if rrow else 0
                if total is not None and (total - sold - reserved) <= 0:
                    return False

            await self._conn.execute(
                "INSERT INTO stock_reservations (key, sku_id, expires_at, job_id, created_at) VALUES (?, ?, ?, NULL, ?)",
                (key, sku_id, expires_at, now),
            )
            await self._conn.commit()
            return True

    async def attach_job(self, key: str, job_id: str, extend_ttl_seconds: int | None = None) -> None:
        assert self._conn is not None
        if extend_ttl_seconds is not None:
            new_expires = int(time.time()) + extend_ttl_seconds
            await self._conn.execute(
                "UPDATE stock_reservations SET job_id = ?, expires_at = ? WHERE key = ?",
                (job_id, new_expires, key),
            )
        else:
            await self._conn.execute(
                "UPDATE stock_reservations SET job_id = ? WHERE key = ?",
                (job_id, key),
            )
        await self._conn.commit()

    async def release(self, key: str, reason: str = "release") -> None:
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM stock_reservations WHERE key = ?", (key,),
        )
        await self._conn.commit()

    async def commit_sold(self, key: str, tx_hash: str) -> None:
        """Finalize successful sale: delete reservation, bump sold, ledger entry."""
        assert self._conn is not None
        now = int(time.time())
        async with self._conn.execute(
            "SELECT sku_id, job_id FROM stock_reservations WHERE key = ?", (key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            logger.warning("commit_sold: no reservation for key=%s", key)
            return
        sku_id, job_id = row[0], row[1]
        if self.has_tracked_stock(sku_id):
            await self._conn.execute(
                "UPDATE skus SET sold = sold + 1, updated_at = ? WHERE sku_id = ?",
                (now, sku_id),
            )
            await self._conn.execute(
                "INSERT INTO stock_ledger (sku_id, delta, reason, job_id, ts) VALUES (?, -1, ?, ?, ?)",
                (sku_id, f"sold:{tx_hash}", job_id, now),
            )
        await self._conn.execute(
            "DELETE FROM stock_reservations WHERE key = ?", (key,),
        )
        await self._conn.commit()

    async def agent_out_of_stock(self, key: str, job_id: str | None = None) -> str | None:
        """Agent reported unit is gone: drop reservation, decrement total, ledger.

        Returns sku_id that was affected, or None if no reservation existed.
        """
        assert self._conn is not None
        now = int(time.time())
        async with self._conn.execute(
            "SELECT sku_id, job_id FROM stock_reservations WHERE key = ?", (key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            logger.warning("agent_out_of_stock: no reservation for key=%s", key)
            return None
        sku_id, existing_job = row[0], row[1]
        job_id = job_id or existing_job
        if self.has_tracked_stock(sku_id):
            # Decrement total (but not below sold — floor at sold count).
            await self._conn.execute(
                """
                UPDATE skus SET total = CASE
                    WHEN total IS NULL THEN NULL
                    WHEN total > sold THEN total - 1
                    ELSE total
                END, updated_at = ? WHERE sku_id = ?
                """,
                (now, sku_id),
            )
            await self._conn.execute(
                "INSERT INTO stock_ledger (sku_id, delta, reason, job_id, ts) VALUES (?, -1, ?, ?, ?)",
                (sku_id, f"out_of_stock:agent", job_id, now),
            )
        await self._conn.execute(
            "DELETE FROM stock_reservations WHERE key = ?", (key,),
        )
        await self._conn.commit()
        return sku_id

    async def sweep_expired(self, now: int | None = None) -> int:
        """Delete reservations whose TTL passed AND no job is attached.

        Reservations with a job_id are left alone — the job finalization path
        owns their lifecycle (commit_sold / release).
        """
        assert self._conn is not None
        now = int(time.time()) if now is None else now
        cursor = await self._conn.execute(
            "DELETE FROM stock_reservations WHERE expires_at <= ? AND job_id IS NULL",
            (now,),
        )
        deleted = cursor.rowcount or 0
        await self._conn.commit()
        return deleted

    # ── Admin (CLI) ────────────────────────────────────────────────

    async def set_total(self, sku_id: str, total: int | None, reason: str) -> None:
        assert self._conn is not None
        await self._require_sku(sku_id)
        now = int(time.time())
        async with self._conn.execute(
            "SELECT total FROM skus WHERE sku_id = ?", (sku_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        prev = row[0]
        await self._conn.execute(
            "UPDATE skus SET total = ?, updated_at = ? WHERE sku_id = ?",
            (total, now, sku_id),
        )
        delta = 0 if (prev is None or total is None) else (total - prev)
        await self._conn.execute(
            "INSERT INTO stock_ledger (sku_id, delta, reason, job_id, ts) VALUES (?, ?, ?, NULL, ?)",
            (sku_id, delta, f"adjust:{reason}", now),
        )
        await self._conn.commit()

    async def adjust_total(self, sku_id: str, delta: int, reason: str) -> int | None:
        """Add `delta` to total. Returns new total, or None if total is NULL."""
        assert self._conn is not None
        await self._require_sku(sku_id)
        if delta == 0:
            view = await self.get_view(sku_id)
            return view.total
        now = int(time.time())
        async with self._conn.execute(
            "SELECT total FROM skus WHERE sku_id = ?", (sku_id,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        prev = row[0]
        if prev is None:
            raise StockError(f"SKU '{sku_id}' has infinite stock; use set_total to start tracking")
        new_total = max(prev + delta, 0)
        await self._conn.execute(
            "UPDATE skus SET total = ?, updated_at = ? WHERE sku_id = ?",
            (new_total, now, sku_id),
        )
        await self._conn.execute(
            "INSERT INTO stock_ledger (sku_id, delta, reason, job_id, ts) VALUES (?, ?, ?, NULL, ?)",
            (sku_id, delta, f"adjust:{reason}", now),
        )
        await self._conn.commit()
        return new_total
