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
from chains.solana.rpc import SolanaRpc, SolanaRpcError

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
    amount: int          # lamports: seller balance delta inside this tx
    nonce: str | None    # pub_nonce from the CTLX:PAY memo
    block_time: int | None
    failed: bool         # meta.err set — tx landed but did not execute


def _account_keys(message: dict[str, Any]) -> tuple[list[str], set[str]]:
    """jsonParsed ``accountKeys`` (incl. lookup-table loads) as base58 strings,
    index-aligned with ``meta.preBalances`` / ``postBalances``, plus signers."""
    keys: list[str] = []
    signers: set[str] = set()
    for k in message.get("accountKeys") or []:
        key = str(k.get("pubkey") if isinstance(k, dict) else k)
        keys.append(key)
        if isinstance(k, dict) and k.get("signer"):
            signers.add(key)
    return keys, signers


def _all_instructions(tx: dict[str, Any], message: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level instructions, then every CPI (``meta.innerInstructions``)."""
    out = [ix for ix in message.get("instructions") or [] if isinstance(ix, dict)]
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        out.extend(ix for ix in (group or {}).get("instructions") or [] if isinstance(ix, dict))
    return out


def parse_sol_payment(tx: dict[str, Any], *, signature: str, pay_to: str) -> SolPayment:
    """Read a jsonParsed tx.

    ``amount`` is the seller's balance delta inside this tx
    (``postBalances - preBalances``), not a sum of parsed transfers: it also
    counts SOL moved by CPI (smart wallets such as Squads) or by a program
    debiting an account it owns, which leaves no transfer instruction at all.
    The seller is a plain system account, so nothing in the tx can debit it
    without our key — the delta is exactly what we received.

    ``sender`` (refund target) is the source of the first transfer to us,
    top-level or CPI; failing that (a program moved lamports directly, e.g.
    closing a wSOL account to us) the signer whose balance dropped most — never
    a program-owned account, which nobody could spend a refund from.
    ``nonce`` comes from the first ``CTLX:PAY`` memo, top-level or CPI — one
    tx pays for one purchase.
    """
    meta = tx.get("meta") or {}
    message = ((tx.get("transaction") or {}).get("message")) or {}
    keys, signers = _account_keys(message)
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []

    def delta(i: int) -> int:
        try:
            return int(post[i]) - int(pre[i])
        except (IndexError, TypeError, ValueError):
            return 0

    amount = max(delta(keys.index(pay_to)), 0) if pay_to in keys else 0

    sender = ""
    nonce: str | None = None
    for ix in _all_instructions(tx, message):
        parsed = ix.get("parsed")
        if ix.get("programId") in _MEMO_PROGRAM_IDS or ix.get("program") == "spl-memo":
            if nonce is None:
                nonce = extract_pay_nonce(parsed)
            continue
        if sender or ix.get("program") != "system" or not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        if parsed.get("type") in ("transfer", "transferWithSeed") and info.get("destination") == pay_to:
            sender = str(info.get("source") or "")
    if not sender and amount > 0:
        drops = [(delta(i), k) for i, k in enumerate(keys)
                 if k != pay_to and k in signers and delta(i) < 0]
        if drops:
            sender = min(drops)[1]
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
    # Rows with no top-level memo are fetched to look for a CPI memo (Squads
    # and other smart wallets: RPC's ``memo`` field shows top-level only).
    # Capped so memo-less dust cannot turn one poll into an RPC flood.
    MAX_TX_FETCHES = 20
    # Lazy polling: every ``poll_interval`` only while a payment is expected
    # (a 402 was issued, see ``expect``) or after a failed poll; otherwise every
    # ``idle_poll_interval``. A claim needs no poll at all — ``SolRail.verify``
    # reads the buyer's signature and forces a poll itself — so an idle agent
    # costs ~9k RPC credits/month instead of ~260k.
    # While polls succeed, health tolerates the idle gap plus this grace.
    IDLE_HEALTH_GRACE = 60.0

    def __init__(
        self,
        rpc: SolanaRpc,
        address: str,
        *,
        poll_interval: float = 10.0,
        idle_poll_interval: float = 300.0,
        commitment: str = "confirmed",
    ) -> None:
        self._rpc = rpc
        self._address = address
        self._poll_interval = poll_interval
        self._idle_poll_interval = max(idle_poll_interval, poll_interval)
        self._active_until: float = 0.0
        self._last_failed = False
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

    def expect(self, window_seconds: float) -> None:
        """A payment may land within ``window_seconds``: poll at the fast rate."""
        was_idle = time.time() >= self._active_until
        self._active_until = max(self._active_until, time.time() + window_seconds)
        if was_idle:
            self._force.set()  # pick up the fast rate now, not after the idle sleep

    def current_interval(self) -> float:
        if self._last_failed or time.time() < self._active_until:
            return self._poll_interval
        return self._idle_poll_interval

    def is_healthy(self, max_age_seconds: float = 60.0) -> bool:
        """Last poll succeeded within ``max_age_seconds`` — or, while polls keep
        succeeding, within the idle interval (+grace): an idle gap is not an
        outage. A failed poll drops back to the caller's strict bound."""
        if self._last_ok_at <= 0:
            return False
        if not self._last_failed:
            max_age_seconds = max(max_age_seconds, self._idle_poll_interval + self.IDLE_HEALTH_GRACE)
        return time.time() - self._last_ok_at <= max_age_seconds

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
            try:
                await self._poll_locked()
            except Exception:
                self._last_failed = True
                raise

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
        # A memo-less row needs getTransaction (CPI memo). If that is capped or
        # fails, stop there and park the watermark on the last row done: the
        # next poll resumes at it, so nothing is skipped and order holds.
        fetches = 0
        last_done: str | None = None
        try:
            for row in reversed(fresh):
                if row.get("err") is None:
                    nonce = extract_pay_nonce(row.get("memo"))
                    if nonce is None and row.get("memo") is None:
                        if fetches >= self.MAX_TX_FETCHES:
                            logger.info(
                                "SolanaPaymentMonitor: fetch cap (%d) hit on %s; resuming next poll",
                                self.MAX_TX_FETCHES, self._address,
                            )
                            newest = last_done or self._watermark
                            break
                        fetches += 1
                        nonce = await self._cpi_nonce(row["signature"])
                    if nonce:
                        self._by_nonce.setdefault(nonce, (row["signature"], now))
                last_done = row["signature"]
        except Exception:
            # Keep the progress made; the poll counts as failed (health stale).
            if last_done is not None:
                self._watermark = last_done
            raise
        if newest is not None:
            self._watermark = newest
        for key in [k for k, (_, seen) in self._by_nonce.items() if seen < cutoff]:
            del self._by_nonce[key]
        self._last_ok_at = time.time()
        self._last_failed = False

    async def _cpi_nonce(self, signature: str) -> str | None:
        """CTLX:PAY nonce from a CPI memo. Raises when the tx can't be read yet
        (RPC error / 429 / not visible) so the poll stops and retries it."""
        tx = await self._rpc.get_transaction(signature, self._commitment)
        if tx is None:
            raise SolanaRpcError(f"getTransaction {signature} not visible yet")
        return parse_sol_payment(tx, signature=signature, pay_to=self._address).nonce

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._force.wait(), timeout=self.current_interval())
            except asyncio.TimeoutError:
                pass
            self._force.clear()
            if self._stop.is_set():
                break
            try:
                await self.poll()
            except Exception as exc:
                logger.warning("SolanaPaymentMonitor: poll failed (%s)", exc)
