from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


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

    async def mark_processed(self, tx_hash: str) -> None:
        if not self._conn:
            await self.init()
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO processed_txs (tx_hash, created_at) VALUES (?, ?)",
            (tx_hash, now_iso),
        )
        await self._conn.commit()

        # Run in background. Store history for 30 days.
        task = asyncio.create_task(self.cleanup(older_than_seconds=30 * 24 * 3600))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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
        await self._conn.commit()
