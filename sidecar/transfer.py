from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import json

from pytoniq_core import Cell, begin_cell
from tonutils.clients import LiteBalancer
from tonutils.contracts.wallet import WalletV4R2
from tonutils.types import NetworkGlobalID, PrivateKey
from tonutils.utils import normalize_hash

logger = logging.getLogger(__name__)

HEARTBEAT_OPCODE = 0xAC52AB67
PAYMENT_OPCODE = 0x50415900
REFUND_OPCODE = 0x52464E44


def heartbeat_body(comment: str) -> Cell:
    return (
        begin_cell()
        .store_uint(HEARTBEAT_OPCODE, 32)
        .store_snake_string(comment)
        .end_cell()
    )


def payment_body(nonce: str) -> Cell:
    return (
        begin_cell()
        .store_uint(PAYMENT_OPCODE, 32)
        .store_snake_string(nonce)
        .end_cell()
    )


def refund_body(original_tx_hash: str, reason: str, sidecar_id: str) -> Cell:
    return (
        begin_cell()
        .store_uint(REFUND_OPCODE, 32)
        .store_snake_string(json.dumps({"tx": original_tx_hash, "reason": reason, "sidecar_id": sidecar_id}))
        .end_cell()
    )


def text_comment_body(text: str) -> Cell:
    return (
        begin_cell()
        .store_uint(0, 32)
        .store_snake_string(text)
        .end_cell()
    )


SEND_MAX_RETRIES = 3
SEND_RETRY_DELAYS = [0.5, 2, 5]  # seconds between retries
SEND_TOTAL_BUDGET_SEC = 30
CONFIRM_TIMEOUT_SEC = 10
CONFIRM_POLL_INTERVAL_SEC = 1


class TransferSender:
    def __init__(
        self,
        private_key_hex: str,
        testnet: bool = False,
    ) -> None:
        self._private_key_hex = private_key_hex
        self._network = NetworkGlobalID.TESTNET if testnet else NetworkGlobalID.MAINNET
        self._client: LiteBalancer | None = None
        self._wallet: WalletV4R2 | None = None
        self._lock = asyncio.Lock()

    async def _ensure_initialized(self) -> None:
        if self._client is not None:
            return
        self._client = LiteBalancer.from_network_config(self._network)
        await self._client.connect()
        pk = PrivateKey(bytes.fromhex(self._private_key_hex.removeprefix("0x")))
        self._wallet = WalletV4R2.from_private_key(self._client, pk)
        logger.info("Transfer sender initialized via liteserver (testnet=%s)", self._network == NetworkGlobalID.TESTNET)

    async def _reconnect(self) -> None:
        """Drop current liteserver connection and re-initialize."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            self._wallet = None
        await self._ensure_initialized()

    async def _find_landed_hash(self, target_hashes: set[str]) -> str | None:
        """Look up wallet's recent transactions and return any of `target_hashes`
        whose external-in message was actually included on chain."""
        if not self._client or not self._wallet:
            return None
        try:
            txs = await self._client.get_transactions(self._wallet.address, limit=10)
        except Exception as exc:
            logger.debug("get_transactions failed during confirmation poll: %s", exc)
            return None
        for tx in txs:
            if tx.in_msg is None or not tx.in_msg.is_external:
                continue
            try:
                h = normalize_hash(tx.in_msg)
            except Exception:
                continue
            if h in target_hashes:
                return h
        return None

    async def send(self, destination: str, amount: int, body: Cell) -> str:
        async with self._lock:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + SEND_TOTAL_BUDGET_SEC
            last_exc: Exception | None = None
            submitted_hashes: set[str] = set()

            for attempt in range(SEND_MAX_RETRIES):
                try:
                    await self._ensure_initialized()
                    assert self._wallet is not None

                    # Before sending again, check if a prior attempt's message
                    # landed on chain (avoids double-send when confirmation
                    # poll merely timed out).
                    if submitted_hashes:
                        landed = await self._find_landed_hash(submitted_hashes)
                        if landed:
                            logger.info(
                                "Transfer confirmed before retry: hash=%s", landed,
                            )
                            return landed

                    msg = await self._wallet.transfer(
                        destination=destination,
                        amount=amount,
                        body=body,
                        bounce=False,
                    )
                    tx_hash = msg.normalized_hash
                    submitted_hashes.add(tx_hash)
                    logger.info(
                        "Transfer submitted: hash=%s dest=%s amount=%d (awaiting confirmation)",
                        tx_hash, destination, amount,
                    )

                    confirm_until = min(loop.time() + CONFIRM_TIMEOUT_SEC, deadline)
                    while loop.time() < confirm_until:
                        await asyncio.sleep(CONFIRM_POLL_INTERVAL_SEC)
                        landed = await self._find_landed_hash(submitted_hashes)
                        if landed:
                            logger.info("Transfer confirmed: hash=%s", landed)
                            return landed

                    last_exc = TimeoutError(
                        f"transfer not confirmed within {CONFIRM_TIMEOUT_SEC}s: hash={tx_hash}"
                    )
                    logger.warning(
                        "Transfer not confirmed (attempt %d/%d): hash=%s",
                        attempt + 1, SEND_MAX_RETRIES, tx_hash,
                    )
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Transfer attempt %d/%d failed (dest=%s amount=%d): %s",
                        attempt + 1, SEND_MAX_RETRIES, destination, amount, exc,
                    )

                if attempt >= SEND_MAX_RETRIES - 1:
                    break
                delay = SEND_RETRY_DELAYS[min(attempt, len(SEND_RETRY_DELAYS) - 1)]
                if loop.time() + delay >= deadline:
                    logger.warning("Send budget exhausted, no more retries")
                    break
                await self._reconnect()
                await asyncio.sleep(delay)

            # Final check before giving up: maybe one of our submissions landed
            # in the gap between last poll and now.
            if submitted_hashes:
                landed = await self._find_landed_hash(submitted_hashes)
                if landed:
                    logger.info("Transfer confirmed on final check: hash=%s", landed)
                    return landed

            logger.error(
                "Transfer failed after %d attempts: dest=%s amount=%d",
                SEND_MAX_RETRIES, destination, amount,
            )
            raise last_exc  # type: ignore[misc]

    async def send_jetton(
        self,
        own_jetton_wallet: str,
        destination: str,
        jetton_amount: int,
        forward_payload: Cell | None = None,
        forward_ton_amount: int = 1,
        attached_ton: int = 60_000_000,
    ) -> str:
        """Send jettons by transferring via agent's own jetton wallet.

        Args:
            own_jetton_wallet: Agent's jetton wallet address (not master).
            destination: Recipient's regular TON wallet address.
            jetton_amount: Amount in jetton base units (e.g. micro-USDT).
            forward_payload: Optional payload forwarded to recipient.
            forward_ton_amount: TON attached to recipient notification (nanoton).
            attached_ton: TON attached to cover gas (nanoton).
        """
        from jetton import jetton_transfer_body

        body = jetton_transfer_body(
            destination=destination,
            amount=jetton_amount,
            response_destination=self._get_wallet_address(),
            forward_payload=forward_payload,
            forward_ton_amount=forward_ton_amount,
        )
        return await self.send(own_jetton_wallet, attached_ton, body)

    def _get_wallet_address(self) -> str:
        from tonutils.contracts.wallet import WalletV4R2
        from tonutils.types import PrivateKey
        pk = PrivateKey(bytes.fromhex(self._private_key_hex.removeprefix("0x")))
        wallet = WalletV4R2.from_private_key(None, pk)  # type: ignore[arg-type]
        return wallet.address.to_str(is_user_friendly=True, is_bounceable=False)

    async def rebuild_client(self) -> None:
        """Drop current LiteBalancer and re-init under the send-lock.

        Serialised with send() so it can never tear an in-flight transfer.
        """
        async with self._lock:
            await self._reconnect()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._wallet = None


# Type alias for injection
TransferFn = Callable[[str, int, Cell], Awaitable[str]]
