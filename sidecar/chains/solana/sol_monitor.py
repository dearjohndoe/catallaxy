"""Native SOL payment watcher: polls the seller address, indexes memos by nonce.

Mirror of the TON ``WalletMonitor``: the buyer broadcasts a SystemProgram
transfer + Memo ``CTLX:PAY:<pub_nonce>`` themselves; we only observe it. The
cache maps ``pub_nonce -> signature`` (first confirmed tx wins). Amount and
sender are read from ``getTransaction`` at verify time, not from this index.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from chains.solana.constants import (
    MEMO_PROGRAM,
    MEMO_PROGRAM_V1,
    PAY_MEMO_PREFIX,
)
from chains.solana.rpc import SolanaRpc

logger = logging.getLogger("sidecar")

_MEMO_PROGRAM_IDS = frozenset({str(MEMO_PROGRAM), str(MEMO_PROGRAM_V1)})
# ``getSignaturesForAddress`` renders memos as ``[len] text`` joined by "; ".
_PAY_RE = re.compile(re.escape(PAY_MEMO_PREFIX) + r"([0-9a-fA-F]{8}:[^\s;\"]+)")


def pay_memo(pub_nonce: str) -> str:
    """Memo text the buyer must attach: ``CTLX:PAY:<pub>:<sidecar_id>``."""
    return f"{PAY_MEMO_PREFIX}{pub_nonce}"


def extract_pay_nonce(memo: Any) -> str | None:
    """``pub_nonce`` from a memo string (raw or RPC-rendered), else None."""
    if not isinstance(memo, str):
        return None
    m = _PAY_RE.search(memo)
    return m.group(1) if m else None


@dataclass
class SolPayment:
    signature: str
    sender: str          # source of the first transfer to us; "" if none
    amount: int          # lamports summed over top-level transfers to us
    nonce: str | None    # pub_nonce from the CTLX:PAY memo
    block_time: int | None
    failed: bool         # meta.err set — tx landed but did not execute


def parse_sol_payment(tx: dict[str, Any], *, signature: str, pay_to: str) -> SolPayment:
    """Read a jsonParsed tx. Only top-level instructions count — a transfer or
    memo inside a CPI is ignored (no wallet builds payments that way)."""
    meta = tx.get("meta") or {}
    message = ((tx.get("transaction") or {}).get("message")) or {}
    amount = 0
    sender = ""
    nonce: str | None = None
    for ix in message.get("instructions") or []:
        if not isinstance(ix, dict):
            continue
        parsed = ix.get("parsed")
        if ix.get("programId") in _MEMO_PROGRAM_IDS or ix.get("program") == "spl-memo":
            if nonce is None:
                nonce = extract_pay_nonce(parsed)
            continue
        if ix.get("program") != "system" or not isinstance(parsed, dict):
            continue
        if parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        if info.get("destination") != pay_to:
            continue
        try:
            lamports = int(info.get("lamports") or 0)
        except (TypeError, ValueError):
            continue
        amount += lamports
        if not sender:
            sender = str(info.get("source") or "")
    return SolPayment(
        signature=signature,
        sender=sender,
        amount=amount,
        nonce=nonce,
        block_time=tx.get("blockTime"),
        failed=meta.get("err") is not None,
    )


class SolanaPaymentMonitor:
    CACHE_TTL = 600     # seconds — same window as the TON monitor
    PAGE_LIMIT = 100
    MAX_PAGES = 5       # per poll; the claim's proof hint covers overflow

    def __init__(
        self,
        rpc: SolanaRpc,
        address: str,
        *,
        poll_interval: float = 10.0,
        commitment: str = "confirmed",
    ) -> None:
        self._rpc = rpc
        self._address = address
        self._poll_interval = poll_interval
        self._commitment = commitment
        self._by_nonce: dict[str, tuple[str, float]] = {}
        self._watermark: str | None = None
        self._last_ok_at: float = 0.0
        self._force = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            await self.poll()
        except Exception:
            logger.exception("SolanaPaymentMonitor: initial poll failed")
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._force.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def force(self) -> None:
        self._force.set()

    def is_healthy(self, max_age_seconds: float = 60.0) -> bool:
        return self._last_ok_at > 0 and time.time() - self._last_ok_at <= max_age_seconds

    def get(self, nonce: str) -> str | None:
        row = self._by_nonce.get(nonce)
        return row[0] if row else None

    def remember(self, nonce: str, signature: str) -> None:
        """Index a tx found outside the poll (claim proof hint)."""
        self._by_nonce.setdefault(nonce, (signature, time.time()))

    def consume(self, nonce: str) -> None:
        self._by_nonce.pop(nonce, None)

    async def poll(self) -> None:
        async with self._lock:
            await self._poll_locked()

    async def _poll_locked(self) -> None:
        now = time.time()
        cutoff = now - self.CACHE_TTL
        newest: str | None = None
        before: str | None = None
        fresh: list[dict[str, Any]] = []
        for page in range(self.MAX_PAGES):
            rows = await self._rpc.get_signatures_for_address(
                self._address,
                limit=self.PAGE_LIMIT,
                before=before,
                until=self._watermark,
                commitment=self._commitment,
            )
            if not rows:
                break
            if newest is None:
                newest = rows[0]["signature"]
            reached_cutoff = False
            for row in rows:
                block_time = row.get("blockTime")
                if isinstance(block_time, int) and block_time < cutoff:
                    reached_cutoff = True
                    break
                fresh.append(row)
            if reached_cutoff or len(rows) < self.PAGE_LIMIT:
                break
            before = rows[-1]["signature"]
            if page == self.MAX_PAGES - 1:
                logger.warning(
                    "SolanaPaymentMonitor: >%d new txs in one poll on %s; older ones "
                    "are only reachable via the claim proof hint",
                    self.MAX_PAGES * self.PAGE_LIMIT, self._address,
                )
        # Oldest first: the first tx carrying a memo owns that nonce. A later
        # copy of the (now public) memo — e.g. dust sent to hijack the claim —
        # must not displace the real payment, even inside one poll batch.
        for row in reversed(fresh):
            if row.get("err") is not None:
                continue
            nonce = extract_pay_nonce(row.get("memo"))
            if nonce:
                self._by_nonce.setdefault(nonce, (row["signature"], now))
        if newest is not None:
            self._watermark = newest
        for key in [k for k, (_, seen) in self._by_nonce.items() if seen < cutoff]:
            del self._by_nonce[key]
        self._last_ok_at = time.time()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._force.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
            self._force.clear()
            if self._stop.is_set():
                break
            try:
                await self.poll()
            except Exception as exc:
                logger.warning("SolanaPaymentMonitor: poll failed (%s)", exc)
