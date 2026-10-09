from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite


class ClaimSecretStore:
    """Persistent store backing the split-nonce claim-secret check.

    Minted alongside each 402 response (`build_402_response`): one row
    `(pub, sha256(sec).hexdigest(), expires_at)` per issued nonce. The claim
    path (`handle_invoke`) looks up `pub` and compares the presented `sec`'s
    hash — a plain, non-destructive read.

    `check()` deliberately does NOT delete on read. `verify_payment` polls
    the chain with its own timeout (`ton_verifier.VERIFY_TIMEOUT`=15s local /
    `REMOTE_VERIFY_TIMEOUT`=50s relay) and can legitimately time out before
    the tx is indexed; the protocol assumes the client retries the *same*
    `POST /invoke {tx, nonce}` afterwards. A secret consumed on first read
    would 403 that legitimate retry — this was an earlier design mistake,
    caught in review (see TODO-claim-auth.md "Mechanism" / "Claim path").
    Exactly-once delivery is not this store's job; it already belongs to
    `ProcessedTxStore.mark_processed`'s PRIMARY KEY.

    Same DB file as `ProcessedTxStore`/`RefundQueue`/`FreeClaimStore`
    (`settings.tx_db_path`) — own `aiosqlite` connection, WAL,
    `busy_timeout=15000`, same idiom as the other three stores in this
    package.
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        # Guards lazy (re-)init so two concurrent first-callers (e.g. if
        # startup's init() failed/was skipped) can't each open their own
        # aiosqlite connection and leak one — see class docstring.
        self._init_lock = asyncio.Lock()

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=15000")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS claim_secrets (
                pub         TEXT PRIMARY KEY,
                secret_hash TEXT NOT NULL,
                expires_at  INTEGER NOT NULL
            )
            """
        )
        await self._conn.commit()

    async def _ensure_conn(self) -> None:
        if self._conn:
            return
        async with self._init_lock:
            if not self._conn:  # re-check: someone may have won the race while we waited
                await self.init()

    async def insert(self, pub: str, secret_hash: str, expires_at: int) -> None:
        """Register a freshly-minted claim secret (called at 402-mint time).

        INSERT OR REPLACE: `pub` is uuid4-derived so a collision is
        vanishingly unlikely, but a retried preflight against the same `pub`
        should refresh the row rather than raise.
        """
        await self._ensure_conn()
        await self._conn.execute(
            "INSERT OR REPLACE INTO claim_secrets (pub, secret_hash, expires_at) "
            "VALUES (?, ?, ?)",
            (pub, secret_hash, expires_at),
        )
        await self._conn.commit()

    async def check(self, pub: str) -> tuple[str, int] | None:
        """Plain read-only lookup: ``(secret_hash, expires_at)`` or ``None``.

        Deliberately non-destructive — see class docstring. Never deletes;
        callers compare ``secret_hash`` against the presented secret's hash
        and ``expires_at`` against the current time themselves.
        """
        await self._ensure_conn()
        async with self._conn.execute(
            "SELECT secret_hash, expires_at FROM claim_secrets WHERE pub = ?",
            (pub,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return str(row[0]), int(row[1])

    async def sweep_expired(self, now: int | None = None) -> int:
        """Delete rows whose TTL has passed. Wired into the 60s cleanup loop."""
        await self._ensure_conn()
        now = int(time.time()) if now is None else now
        cursor = await self._conn.execute(
            "DELETE FROM claim_secrets WHERE expires_at <= ?",
            (now,),
        )
        deleted = cursor.rowcount or 0
        await self._conn.commit()
        return deleted

    async def delete(self, pub: str) -> None:
        """Best-effort post-success hygiene — NEVER a security gate.

        Called after `mark_processed` succeeds so a captured secret stops
        being useful sooner than its TTL. Not required for correctness (the
        row would simply expire on its own), so callers should treat this as
        fire-and-forget and wrap the call in try/except.
        """
        await self._ensure_conn()
        await self._conn.execute("DELETE FROM claim_secrets WHERE pub = ?", (pub,))
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
