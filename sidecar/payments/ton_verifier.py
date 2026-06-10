from __future__ import annotations

import asyncio
import logging
import time

from tonutils.clients import LiteBalancer
from tonutils.types import NetworkGlobalID

from .nonce import _parse_payment_nonce, parse_nonce
from .remote_monitor import RemoteWalletMonitor, _RelayClient, get_relay_url
from .ton_monitor import WalletMonitor
from .tonapi_client import TonAPIClient
from .types import PaymentVerificationError, VerifiedPayment

logger = logging.getLogger(__name__)


class PaymentVerifier:
    VERIFY_TIMEOUT = 15   # local mode: liteserver polled directly, fail fast
    # Remote mode: tx arrives via relay (TonAPI webhook). End-to-end latency
    # (on-chain → TonAPI index → webhook delivery → relay get_transaction
    # retries) can run 20-30s, well past the local 15s. Poll the relay longer
    # since it's a cheap localhost call and the relay WILL get the tx.
    REMOTE_VERIFY_TIMEOUT = 50
    VERIFY_POLL    = 0.5  # seconds between cache re-checks while waiting

    def __init__(
        self,
        agent_wallet: str,
        min_amount: int,
        payment_timeout_seconds: int,
        enforce_comment_nonce: bool = True,
        testnet: bool = False,
        tonapi_client: TonAPIClient | None = None,
    ) -> None:
        self._agent_wallet = agent_wallet
        self._min_amount = min_amount
        self._payment_timeout = payment_timeout_seconds
        self._enforce_comment_nonce = enforce_comment_nonce
        self._network = NetworkGlobalID.TESTNET if testnet else NetworkGlobalID.MAINNET
        self._client: LiteBalancer | None = None
        self._monitor: WalletMonitor | RemoteWalletMonitor | None = None
        self._tonapi_client = tonapi_client
        # Shared aiohttp session for relay client, only used in remote mode.
        self._relay_client: _RelayClient | None = None

    async def start(self) -> None:
        relay_url = get_relay_url()
        if relay_url:
            self._relay_client = _RelayClient(relay_url)
            await self._relay_client.subscribe(
                agent_wallet=self._agent_wallet,
                jetton_wallet=None,
                label=None,
            )
            self._monitor = RemoteWalletMonitor(self._relay_client, self._agent_wallet)
            await self._monitor.start()
            logger.info("PaymentVerifier started in REMOTE mode via %s", relay_url)
            return

        self._client = LiteBalancer.from_network_config(self._network)
        await self._client.connect()
        self._monitor = WalletMonitor(
            self._client, self._agent_wallet, tonapi_client=self._tonapi_client,
        )
        await self._monitor.start()
        logger.info("PaymentVerifier started (testnet=%s)", self._network == NetworkGlobalID.TESTNET)

    async def close(self) -> None:
        if self._monitor:
            await self._monitor.stop()
            self._monitor = None
        if self._client:
            await self._client.close()
            self._client = None
        if self._relay_client:
            await self._relay_client.close()
            self._relay_client = None

    def is_healthy(self, max_age_seconds: float = 60.0) -> bool:
        """Proxy to monitor.is_healthy. Unstarted verifier is never healthy."""
        if self._monitor is None:
            return False
        return self._monitor.is_healthy(max_age_seconds)

    async def rebuild_client(self) -> None:
        """Periodically swap the LiteBalancer to shed long-lived state.

        Monitor's `_by_nonce` cache and `_last_processed_lt` are preserved,
        so inflight verify() loses at most one poll cycle. In remote mode
        there's no LiteBalancer to rebuild — this is a no-op.
        """
        if self._monitor is None or self._client is None:
            return
        new_client = LiteBalancer.from_network_config(self._network)
        await new_client.connect()
        old = self._client
        await self._monitor.replace_client(new_client)
        self._client = new_client
        if old is not None:
            try:
                await old.close()
            except Exception:
                logger.exception("PaymentVerifier.rebuild_client: old client close failed")

    async def verify(self, tx_hash: str, raw_nonce: str, min_amount: int | None = None) -> VerifiedPayment:
        if self._monitor is None:
            raise RuntimeError("PaymentVerifier not started")

        nonce = parse_nonce(raw_nonce)
        required_amount = min_amount if min_amount is not None else self._min_amount
        timeout = self.REMOTE_VERIFY_TIMEOUT if self._relay_client is not None else self.VERIFY_TIMEOUT
        deadline = time.time() + timeout

        while True:
            tx = await self._monitor.get(nonce.value)

            if tx is not None:
                now_ts = int(time.time())
                if now_ts - tx.now > self._payment_timeout:
                    raise PaymentVerificationError("Payment session expired")

                try:
                    sender = tx.in_msg.info.src.to_str(is_user_friendly=True, is_bounceable=False)
                except Exception:
                    sender = ""

                try:
                    amount = int(tx.in_msg.info.value.grams)
                except Exception:
                    amount = 0

                if amount < required_amount:
                    raise PaymentVerificationError("Transaction amount is lower than required price")

                if not sender:
                    raise PaymentVerificationError("Transaction sender is missing")

                comment = _parse_payment_nonce(tx.in_msg.body) or nonce.value
                # Evict nonce from cache and use the on-chain tx hash (not user-supplied)
                # to prevent replay attacks with fake tx_hash values.
                await self._monitor.consume(nonce.value)
                real_tx_hash = tx.cell.hash.hex()
                return VerifiedPayment(
                    tx_hash=real_tx_hash,
                    sender=sender,
                    recipient=self._agent_wallet,
                    amount=amount,
                    comment=comment,
                )

            if time.time() >= deadline:
                raise PaymentVerificationError("Transaction not found")

            # Not in cache yet — force an immediate poll, then wait before retrying.
            # In remote mode `force()` is a no-op (relay polls on its own).
            self._monitor.force()
            await asyncio.sleep(self.VERIFY_POLL)
