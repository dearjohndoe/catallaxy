from __future__ import annotations

import asyncio
import logging
import time

from tonutils.clients import LiteBalancer
from tonutils.types import NetworkGlobalID

from .jetton_monitor import JettonWalletMonitor
from .nonce import parse_nonce
from .remote_monitor import RemoteJettonWalletMonitor, _RelayClient, get_relay_url
from .tonapi_client import TonAPIClient
from .types import PaymentVerificationError, VerifiedPayment

logger = logging.getLogger(__name__)


class JettonPaymentVerifier:
    """Verifies incoming jetton (USDT) payments on the agent wallet."""

    VERIFY_TIMEOUT = 15
    REMOTE_VERIFY_TIMEOUT = 50
    VERIFY_POLL = 0.5

    def __init__(
        self,
        agent_wallet: str,
        usdt_master: str,
        min_amount: int,
        payment_timeout_seconds: int,
        testnet: bool = False,
        tonapi_client: TonAPIClient | None = None,
    ) -> None:
        self._agent_wallet = agent_wallet
        self._usdt_master = usdt_master
        self._min_amount = min_amount
        self._payment_timeout = payment_timeout_seconds
        self._network = NetworkGlobalID.TESTNET if testnet else NetworkGlobalID.MAINNET
        self._client: LiteBalancer | None = None
        self._monitor: JettonWalletMonitor | RemoteJettonWalletMonitor | None = None
        self.jetton_wallet_address: str = ""
        self._tonapi_client = tonapi_client
        self._relay_client: _RelayClient | None = None

    async def start(self) -> None:
        from tonutils.contracts.jetton.master import JettonMasterStablecoin
        from jetton import USDT_JETTON_WALLET_CODE_HEX

        addr = JettonMasterStablecoin.calculate_user_jetton_wallet_address(
            owner_address=self._agent_wallet,
            jetton_master_address=self._usdt_master,
            jetton_wallet_code=USDT_JETTON_WALLET_CODE_HEX,
        )
        self.jetton_wallet_address = addr.to_str(
            is_user_friendly=True, is_bounceable=False,
        )

        relay_url = get_relay_url()
        if relay_url:
            # Remote mode — relay watches the jetton wallet for us. No LiteBalancer
            # involved at any point.
            self._relay_client = _RelayClient(relay_url)
            await self._relay_client.subscribe(
                agent_wallet=None,
                jetton_wallet=self.jetton_wallet_address,
                label=None,
            )
            self._monitor = RemoteJettonWalletMonitor(
                self._relay_client, self.jetton_wallet_address,
            )
            await self._monitor.start()
            logger.info(
                "JettonPaymentVerifier started in REMOTE mode: jetton_wallet=%s",
                self.jetton_wallet_address,
            )
            return

        # Local mode — open LiteBalancer for the polling JettonWalletMonitor.
        self._client = LiteBalancer.from_network_config(self._network)
        await self._client.connect()
        logger.info(
            "JettonPaymentVerifier started: jetton_wallet=%s (testnet=%s)",
            self.jetton_wallet_address,
            self._network == NetworkGlobalID.TESTNET,
        )

        self._monitor = JettonWalletMonitor(
            self._client,
            self._agent_wallet,
            self.jetton_wallet_address,
            tonapi_client=self._tonapi_client,
        )
        await self._monitor.start()

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
        """Swap LiteBalancer; jetton monitor cache survives the swap.
        No-op in remote mode (no LiteBalancer to rebuild)."""
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
                logger.exception("JettonPaymentVerifier.rebuild_client: old client close failed")

    async def verify(self, tx_hash: str, raw_nonce: str, min_amount: int | None = None) -> VerifiedPayment:
        if self._monitor is None:
            raise RuntimeError("JettonPaymentVerifier not started")

        nonce = parse_nonce(raw_nonce)
        required_amount = min_amount if min_amount is not None else self._min_amount
        timeout = self.REMOTE_VERIFY_TIMEOUT if self._relay_client is not None else self.VERIFY_TIMEOUT
        deadline = time.time() + timeout

        while True:
            entry = await self._monitor.get(nonce.value)

            if entry is not None:
                now_ts = int(time.time())
                if now_ts - entry.tx.now > self._payment_timeout:
                    raise PaymentVerificationError("Payment session expired")

                if entry.amount < required_amount:
                    raise PaymentVerificationError("Transaction amount is lower than required price")

                if not entry.sender:
                    raise PaymentVerificationError("Transaction sender is missing")

                await self._monitor.consume(nonce.value)
                real_tx_hash = entry.tx.cell.hash.hex()
                return VerifiedPayment(
                    tx_hash=real_tx_hash,
                    sender=entry.sender,
                    recipient=self._agent_wallet,
                    amount=entry.amount,
                    comment=entry.nonce,
                )

            if time.time() >= deadline:
                raise PaymentVerificationError("Transaction not found")

            self._monitor.force()
            await asyncio.sleep(self.VERIFY_POLL)
