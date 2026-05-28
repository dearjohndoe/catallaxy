from __future__ import annotations

import asyncio
import logging
import time

from tonutils.clients import LiteBalancer

from jetton import parse_transfer_notification

from .nonce import _parse_payment_nonce
from .tonapi_client import TonAPIClient, TonAPIRateLimitError
from .types import JettonPaymentTx

logger = logging.getLogger(__name__)


class JettonWalletMonitor:
    """Background worker that polls the agent wallet for incoming jetton
    transfer_notification messages and caches them by payment nonce."""

    CACHE_TTL = 600

    def __init__(
        self,
        client: LiteBalancer,
        agent_address: str,
        jetton_wallet_address: str,
        poll_interval: int = 30,
        tonapi_client: TonAPIClient | None = None,
    ) -> None:
        self._client = client
        self._address = agent_address
        self._jetton_wallet = jetton_wallet_address
        self._poll_interval = poll_interval
        self._by_nonce: dict[str, JettonPaymentTx] = {}
        self._force = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_processed_lt: int = 0
        self._tonapi_client = tonapi_client
        self._last_successful_poll_at: float = 0.0
        self._consecutive_lite_errors: int = 0
        self._last_loop_tick: float = 0.0

    async def start(self) -> None:
        await self._poll()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._force.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def replace_client(self, client: LiteBalancer) -> None:
        """Swap underlying liteserver client without losing cache state.

        See WalletMonitor.replace_client for the rationale.
        """
        if self._task is not None:
            self._stop.set()
            self._force.set()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._stop = asyncio.Event()
        self._force = asyncio.Event()
        self._client = client
        self._task = asyncio.create_task(self._loop())

    def force(self) -> None:
        self._force.set()

    def is_healthy(self, max_age_seconds: float = 120.0) -> bool:
        """See WalletMonitor.is_healthy."""
        if self._last_successful_poll_at == 0.0:
            return False
        return (time.time() - self._last_successful_poll_at) < max_age_seconds

    async def get(self, nonce: str) -> JettonPaymentTx | None:
        return self._by_nonce.get(nonce.strip())

    async def consume(self, nonce: str) -> JettonPaymentTx | None:
        return self._by_nonce.pop(nonce.strip(), None)

    def _ingest_txs(self, txs, cutoff: float, new_lt_watermark: int) -> int:
        """Process a batch of txs (newest-first). Returns updated watermark."""
        for tx in txs:
            if tx.lt <= self._last_processed_lt:
                break
            if tx.now < cutoff:
                break
            if tx.lt > new_lt_watermark:
                new_lt_watermark = tx.lt
            if tx.in_msg is None:
                continue

            try:
                src = tx.in_msg.info.src.to_str(
                    is_user_friendly=True, is_bounceable=False,
                )
            except Exception:
                continue
            if src != self._jetton_wallet:
                continue

            notification = parse_transfer_notification(tx.in_msg.body)
            if notification is None:
                continue

            nonce = _parse_payment_nonce(notification.forward_payload)
            if not nonce:
                continue

            self._by_nonce[nonce.strip()] = JettonPaymentTx(
                tx=tx,
                amount=notification.amount,
                sender=notification.sender,
                nonce=nonce,
            )
        return new_lt_watermark

    async def _poll_adnl(self, cutoff: float, new_lt_watermark: int) -> int:
        current_lt: int | None = None
        while True:
            kwargs: dict = {"limit": 50}
            if current_lt is not None:
                kwargs["from_lt"] = current_lt
            txs = await self._client.get_transactions(self._address, **kwargs)
            if not txs:
                break
            new_lt_watermark = self._ingest_txs(txs, cutoff, new_lt_watermark)
            last_tx = txs[-1]
            if last_tx.lt <= self._last_processed_lt or last_tx.now < cutoff:
                break
            if current_lt == last_tx.lt:
                break
            current_lt = last_tx.lt
        return new_lt_watermark

    async def _poll_tonapi(self, cutoff: float, new_lt_watermark: int) -> int:
        assert self._tonapi_client is not None
        txs = await self._tonapi_client.get_account_transactions(self._address, limit=50)
        if txs:
            new_lt_watermark = self._ingest_txs(txs, cutoff, new_lt_watermark)
        return new_lt_watermark

    async def _poll(self) -> None:
        cutoff = time.time() - self.CACHE_TTL
        new_lt_watermark = self._last_processed_lt
        adnl_ok = False
        tonapi_ok = False
        try:
            try:
                new_lt_watermark = await self._poll_adnl(cutoff, new_lt_watermark)
                adnl_ok = True
                self._consecutive_lite_errors = 0
            except Exception as e:
                self._consecutive_lite_errors += 1
                if self._tonapi_client is None:
                    logger.warning(
                        "JettonWalletMonitor: ADNL fetch failed (%s); no TonAPI fallback", e,
                    )
                else:
                    logger.warning(
                        "JettonWalletMonitor: ADNL fetch failed (%s); falling back to TonAPI", e,
                    )
                    try:
                        new_lt_watermark = await self._poll_tonapi(cutoff, new_lt_watermark)
                        tonapi_ok = True
                        logger.info("JettonWalletMonitor: TonAPI fallback poll succeeded")
                    except TonAPIRateLimitError as rl:
                        logger.warning("JettonWalletMonitor: TonAPI fallback %s", rl)
                    except Exception:
                        logger.exception("JettonWalletMonitor: TonAPI fallback failed too")
        finally:
            if new_lt_watermark > self._last_processed_lt:
                self._last_processed_lt = new_lt_watermark
            if adnl_ok or tonapi_ok:
                self._last_successful_poll_at = time.time()
            for k, entry in list(self._by_nonce.items()):
                if entry.tx.now < cutoff:
                    del self._by_nonce[k]

    async def _loop(self) -> None:
        cooldown = 2.0
        poll_hard_timeout = max(self._poll_interval * 3, 30)
        while not self._stop.is_set():
            self._last_loop_tick = time.time()
            self._force.clear()
            try:
                await asyncio.wait_for(self._force.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            start_ts = time.time()
            try:
                await asyncio.wait_for(self._poll(), timeout=poll_hard_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "JettonWalletMonitor: _poll hard timeout after %ss, skipping cycle",
                    poll_hard_timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JettonWalletMonitor: _poll raised, loop continues")
            elapsed = time.time() - start_ts
            if elapsed < cooldown:
                await asyncio.sleep(cooldown - elapsed)
