"""Native SOL rail: buyer broadcasts transfer + Memo ``CTLX:PAY:<pub_nonce>``.

TON mirror (split-nonce, verify by ``pub`` via our own watcher, proof = tx
signature used only as a lookup hint). No facilitator: the buyer's wallet
publishes the tx, the sidecar only observes it and refunds from the seller key.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Callable

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

from chains.solana.constants import (
    MEMO_PROGRAM,
    RENT_EXEMPT_MIN_LAMPORTS,
    SIGNATURE_FEE_LAMPORTS,
)
from chains.solana.refunds import find_refund_signature, refund_memo
from chains.solana.rpc import SolanaRpc
from chains.solana.sol_monitor import SolanaPaymentMonitor, SolPayment, parse_sol_payment, pay_memo
from payments.types import PaymentVerificationError, VerifiedPayment, underpaid_error

logger = logging.getLogger("sidecar")


def _looks_like_signature(value: str) -> bool:
    try:
        Signature.from_string(value)
        return True
    except Exception:
        return False


class SolRail:
    rail_id = "SOL"

    VERIFY_TIMEOUT = 30.0  # confirmed lands in ~1-2s; RPC index lag on top
    VERIFY_POLL = 1.0

    def __init__(
        self,
        *,
        keypair: Keypair,
        rpc: SolanaRpc,
        cluster: str,
        get_sidecar_id: Callable[[], str],
        payment_timeout_seconds: int,
        refund_fee_lamports: int = SIGNATURE_FEE_LAMPORTS,
        commitment: str = "confirmed",
        monitor: SolanaPaymentMonitor | None = None,
    ) -> None:
        self._kp = keypair
        self._rpc = rpc
        self.cluster = "devnet" if cluster == "devnet" else "mainnet"
        self.pay_to = keypair.pubkey()
        self._get_sidecar_id = get_sidecar_id
        self._payment_timeout = payment_timeout_seconds
        self._refund_fee = refund_fee_lamports
        self._commitment = commitment
        self.monitor = monitor or SolanaPaymentMonitor(
            rpc, str(self.pay_to), commitment=commitment,
        )

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.monitor.start()

    async def close(self) -> None:
        await self.monitor.stop()
        await self._rpc.close()

    # ── ChainRail ────────────────────────────────────────────────────────

    async def verify(self, proof: str, nonce: str, min_amount: int) -> VerifiedPayment:
        """Find the payment by ``nonce`` (the memo), never trust ``proof``.

        ``proof`` (buyer's signature) is only a hint for when the poller has
        not caught up: it is accepted iff that tx carries this exact memo.
        """
        nonce = nonce.strip()
        hint = proof.strip() if proof and _looks_like_signature(proof.strip()) else None
        deadline = time.time() + self.VERIFY_TIMEOUT
        while True:
            payment: SolPayment | None = None
            sig = self.monitor.get(nonce)
            if sig is not None:
                payment = await self._fetch(sig)
                if (
                    payment is not None and hint is not None and hint != sig
                    and 0 < payment.amount < min_amount
                ):
                    # The memo's first tx underpaid (refunded separately); the
                    # claimant points at a top-up under the same memo.
                    topup = await self._fetch(hint)
                    if (
                        topup is not None and topup.nonce == nonce
                        and not topup.failed and topup.amount >= min_amount
                    ):
                        payment = topup
            elif hint is not None:
                payment = await self._fetch(hint)
                if payment is not None:
                    if payment.nonce == nonce and not payment.failed:
                        self.monitor.remember(nonce, hint)
                    else:
                        payment, hint = None, None  # wrong / failed tx: drop the hint
            if payment is not None:
                return self._accept(payment, min_amount)
            if time.time() >= deadline:
                raise PaymentVerificationError("Transaction not found")
            self.monitor.force()
            await asyncio.sleep(self.VERIFY_POLL)

    def _accept(self, payment: SolPayment, min_amount: int) -> VerifiedPayment:
        if payment.failed:
            raise PaymentVerificationError("Transaction failed on-chain")
        if payment.block_time is not None and time.time() - payment.block_time > self._payment_timeout:
            raise PaymentVerificationError("Payment session expired")
        if payment.amount <= 0 or payment.amount < min_amount:
            raise underpaid_error(
                tx_hash=payment.signature, sender=payment.sender,
                recipient=str(self.pay_to), amount=payment.amount,
                comment=payment.nonce or "", required=min_amount,
            )
        if not payment.sender:
            raise PaymentVerificationError("Transaction sender is missing")
        return VerifiedPayment(
            tx_hash=payment.signature,
            sender=payment.sender,
            recipient=str(self.pay_to),
            amount=payment.amount,
            comment=payment.nonce or "",
        )

    async def _fetch(self, signature: str) -> SolPayment | None:
        tx = await self._rpc.get_transaction(signature, self._commitment)
        if tx is None:
            return None
        return parse_sol_payment(tx, signature=signature, pay_to=str(self.pay_to))

    async def refund(
        self, to: str, amount: int, *, original_tx_hash: str, reason: str,
    ) -> str | None:
        refund_amount = max(amount - self._refund_fee, 0)
        if refund_amount <= 0:
            logger.warning(
                "SOL refund skipped: amount %s <= fee %s tx=%s",
                amount, self._refund_fee, original_tx_hash,
            )
            return None
        try:
            rent = await self._rpc.min_rent(0, fallback=RENT_EXEMPT_MIN_LAMPORTS)
            if refund_amount < rent and await self._rpc.get_account_info(to) is None:
                # Would create an account below rent-exempt: the runtime rejects it.
                logger.warning(
                    "SOL refund skipped: %s lamports cannot fund new account %s tx=%s",
                    refund_amount, to, original_tx_hash,
                )
                return None
            memo = refund_memo(original_tx_hash, reason, self._get_sidecar_id())
            return await self._send_sol(to, refund_amount, memo)
        except Exception:
            logger.exception("Failed to send SOL refund tx=%s", original_tx_hash)
            return None

    def payment_option(self, amount: int, nonce: str) -> dict[str, Any]:
        # ``memo`` is the literal Memo-program text; ``nonce`` (with sec) is
        # added by the caller and must never go on-chain.
        return {
            "rail": "SOL",
            "address": str(self.pay_to),
            "amount": str(amount),
            "memo": pay_memo(nonce),
            "network": self.cluster,
        }

    def monitor_healthy(self, max_age_seconds: float = 60.0) -> bool:
        return self.monitor.is_healthy(max_age_seconds)

    async def consume(self, nonce: str) -> None:
        try:
            self.monitor.consume(nonce.strip())
        except Exception:
            logger.exception("SOL rail consume failed nonce=%s", nonce)

    # ── refund-worker hooks (rail-generic in refund_worker) ──────────────

    async def recover_payment(self, nonce: str) -> tuple[str, int] | None:
        """(sender, amount) for a queued refund whose verify never finished."""
        try:
            await self.monitor.poll()
        except Exception:
            logger.warning("SOL recover_payment: poll failed", exc_info=True)
        sig = self.monitor.get(nonce.strip())
        if sig is None:
            return None
        payment = await self._fetch(sig)
        if payment is None or payment.failed or not payment.sender or payment.amount <= 0:
            return None
        return payment.sender, payment.amount

    async def check_refund_balance(self, amount: int) -> tuple[bool, str]:
        """Seller must stay rent-exempt after sending ``amount - fee`` + tx fee."""
        balance = await self._rpc.get_balance(str(self.pay_to), self._commitment)
        rent = await self._rpc.min_rent(0, fallback=RENT_EXEMPT_MIN_LAMPORTS)
        required = max(amount - self._refund_fee, 0) + SIGNATURE_FEE_LAMPORTS + rent
        if balance < required:
            return False, f"SOL balance {balance} < required {required}"
        return True, ""

    async def find_existing_refund(
        self, original_tx_hash: str, *, since: float | None = None,
    ) -> str | None:
        """Signature of a refund we already sent for ``original_tx_hash``."""
        return await find_refund_signature(
            self._rpc, str(self.pay_to), original_tx_hash,
            commitment=self._commitment, since=since,
        )

    async def _send_sol(self, to: str, lamports: int, memo: str) -> str:
        dest = Pubkey.from_string(to)
        ixs = [
            set_compute_unit_limit(100_000),  # memo cost grows with length
            set_compute_unit_price(1),
            transfer(TransferParams(from_pubkey=self.pay_to, to_pubkey=dest, lamports=lamports)),
            Instruction(MEMO_PROGRAM, memo.encode(), [AccountMeta(self.pay_to, True, False)]),
        ]
        blockhash, _ = await self._rpc.get_latest_blockhash()
        msg = MessageV0.try_compile(self.pay_to, ixs, [], Hash.from_string(blockhash))
        tx = VersionedTransaction(msg, [self._kp])
        return await self._rpc.send_transaction(base64.b64encode(bytes(tx)).decode("ascii"))
