"""USDC-on-Solana rail: x402 exact, we settle as fee payer."""

from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
from typing import Any, Callable

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.system_program import ID as SYSTEM_PROGRAM

from chains.solana.constants import (
    ASSOCIATED_TOKEN_PROGRAM,
    MEMO_PROGRAM,
    RENT_EXEMPT_MIN_LAMPORTS,
    SIGNATURE_FEE_LAMPORTS,
    TOKEN_ACCOUNT_RENT_LAMPORTS,
    TOKEN_ACCOUNT_SIZE,
    TOKEN_PROGRAM,
    USDC_MINT_DEVNET,
    USDC_MINT_MAINNET,
    USDC_REFUND_ATA_FEE,
    USDC_REFUND_FEE,
)
from chains.solana.exact import ExactSettler, InspectedExactPayment, inspect_exact
from chains.solana.refunds import find_refund_signature, refund_memo
from chains.solana.rpc import SolanaRpc
from chains.solana.x402 import cluster_caip2, exact_requirements
from payments.types import VerifiedPayment

logger = logging.getLogger("sidecar")


class UsdcRail:
    rail_id = "USDC"

    # No chain watcher here (we broadcast the buyer's tx ourselves), so health
    # is the RPC we settle through: probed with ``getHealth`` on this cadence.
    HEALTH_PROBE_INTERVAL = 15.0

    def __init__(
        self,
        *,
        keypair: Keypair,
        rpc: SolanaRpc,
        cluster: str,
        mint: str | None = None,
        settler: ExactSettler | None = None,
        get_sidecar_id: Callable[[], str] = lambda: "",
        refund_fee: int = USDC_REFUND_FEE,
        refund_ata_fee: int = USDC_REFUND_ATA_FEE,
    ) -> None:
        self._kp = keypair
        self._refund_fee = refund_fee
        self._refund_ata_fee = refund_ata_fee
        self._rpc = rpc
        self.cluster = "devnet" if cluster == "devnet" else "mainnet"
        self.mint = Pubkey.from_string(
            mint
            or (USDC_MINT_DEVNET if self.cluster == "devnet" else USDC_MINT_MAINNET)
        )
        self.pay_to = keypair.pubkey()
        self._settler = settler or ExactSettler(keypair=keypair, rpc=rpc)
        self._get_sidecar_id = get_sidecar_id
        self._last_ok_at: float = 0.0
        self._stop = asyncio.Event()
        self._probe_task: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.probe()
        self._stop = asyncio.Event()
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def probe(self) -> bool:
        """One ``getHealth``; a node that is behind or unreachable is unhealthy."""
        ok = await self._rpc.get_health()
        if ok:
            self._last_ok_at = time.time()
        return ok

    async def _probe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.HEALTH_PROBE_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.probe()
            except Exception:
                logger.exception("USDC rail: RPC health probe failed")

    @property
    def fee_payer(self) -> str:
        return str(self.pay_to)

    def inspect(self, proof: str, min_amount: int) -> InspectedExactPayment:
        return inspect_exact(
            proof,
            min_amount=min_amount,
            pay_to=self.pay_to,
            mint=self.mint,
            fee_payer=self.pay_to,
            cluster=self.cluster,
        )

    async def verify(self, proof: str, nonce: str, min_amount: int) -> VerifiedPayment:
        # nonce unused: x402 exact is not in mempool before claim.
        inspected = self.inspect(proof, min_amount)
        return await self._settler.settle(inspected)

    async def settle(self, inspected: InspectedExactPayment) -> VerifiedPayment:
        return await self._settler.settle(inspected)

    async def refund(
        self, to: str, amount: int, *, original_tx_hash: str, reason: str,
    ) -> str | None:
        """Refund ``amount`` minus the fixed fee (plus the account-creation fee
        when the buyer's USDC account is gone). Skipped (None) when the fee
        eats the whole amount — the worker treats that as final."""
        try:
            dest = get_associated_token_address(Pubkey.from_string(to), self.mint, TOKEN_PROGRAM)
            dest_exists = await self._rpc.get_account_info(str(dest)) is not None
            fee = self._refund_fee + (0 if dest_exists else self._refund_ata_fee)
            refund_amount = amount - fee
            if refund_amount <= 0:
                logger.warning(
                    "USDC refund skipped: amount %s <= fee %s (buyer account %s) tx=%s",
                    amount, fee, "exists" if dest_exists else "missing", original_tx_hash,
                )
                return None
            memo = refund_memo(original_tx_hash, reason, self._get_sidecar_id())
            return await self._send_usdc(to, refund_amount, memo, dest_exists=dest_exists)
        except Exception:
            logger.exception("Failed to send USDC refund tx=%s", original_tx_hash)
            return None

    def payment_option(self, amount: int, nonce: str) -> dict[str, Any]:
        # ``memo`` kept for CTLX shape; x402 clients use accepts[] / extra.feePayer.
        return {
            "rail": "USDC",
            "address": str(self.pay_to),
            "amount": str(amount),
            "memo": nonce,
            "token": {"symbol": "USDC", "mint": str(self.mint), "decimals": 6},
            "network": cluster_caip2(self.cluster),
            "extra": {"feePayer": self.fee_payer},
        }

    def x402_accepts_entry(
        self, amount: int, *, resource_url: str, description: str = "",
    ) -> dict[str, Any]:
        return exact_requirements(
            amount=amount,
            pay_to=str(self.pay_to),
            asset=str(self.mint),
            fee_payer=self.fee_payer,
            cluster=self.cluster,
            resource_url=resource_url,
            description=description,
        )

    def monitor_healthy(self, max_age_seconds: float = 60.0) -> bool:
        """Fresh successful ``getHealth`` within ``max_age_seconds``. Unstarted
        rail is never healthy, same as the TON verifiers."""
        return self._last_ok_at > 0 and time.time() - self._last_ok_at <= max_age_seconds

    async def consume(self, nonce: str) -> None:
        return None

    async def close(self) -> None:
        self._stop.set()
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):
                pass
            self._probe_task = None
        await self._rpc.close()

    # ── refund-worker hooks (rail-generic in refund_worker) ──────────────

    async def check_refund_balance(self, amount: int) -> tuple[bool, str]:
        """Seller USDC ATA must hold ``amount``; seller SOL must cover the tx
        fee plus a possible buyer-ATA creation while staying rent-exempt."""
        src = get_associated_token_address(self.pay_to, self.mint, TOKEN_PROGRAM)
        usdc = await self._rpc.get_token_account_balance(str(src))
        if usdc is None or usdc < amount:
            return False, f"USDC balance {usdc or 0} < required {amount}"
        lamports = await self._rpc.get_balance(str(self.pay_to))
        need = (
            SIGNATURE_FEE_LAMPORTS
            + await self._rpc.min_rent(TOKEN_ACCOUNT_SIZE, fallback=TOKEN_ACCOUNT_RENT_LAMPORTS)
            + await self._rpc.min_rent(0, fallback=RENT_EXEMPT_MIN_LAMPORTS)
        )
        if lamports < need:
            return False, f"SOL balance {lamports} < required {need} for USDC refund fees"
        return True, ""

    async def find_existing_refund(self, original_tx_hash: str) -> str | None:
        return await find_refund_signature(self._rpc, str(self.pay_to), original_tx_hash)

    async def _send_usdc(
        self, owner_b58: str, amount: int, memo: str | None = None, *, dest_exists: bool | None = None,
    ) -> str:
        dest_owner = Pubkey.from_string(owner_b58)
        src = get_associated_token_address(self.pay_to, self.mint, TOKEN_PROGRAM)
        dest = get_associated_token_address(dest_owner, self.mint, TOKEN_PROGRAM)
        ixs: list[Instruction] = [
            set_compute_unit_limit(100_000),  # ATA create + transfer + memo
            set_compute_unit_price(1),
        ]
        if dest_exists is None:
            dest_exists = await self._rpc.get_account_info(str(dest)) is not None
        if not dest_exists:
            ixs.append(
                Instruction(
                    ASSOCIATED_TOKEN_PROGRAM,
                    bytes([1]),  # CreateIdempotent
                    [
                        AccountMeta(self.pay_to, True, True),
                        AccountMeta(dest, False, True),
                        AccountMeta(dest_owner, False, False),
                        AccountMeta(self.mint, False, False),
                        AccountMeta(SYSTEM_PROGRAM, False, False),
                        AccountMeta(TOKEN_PROGRAM, False, False),
                    ],
                )
            )
        data = bytes([12]) + struct.pack("<Q", amount) + bytes([6])
        ixs.append(
            Instruction(
                TOKEN_PROGRAM,
                data,
                [
                    AccountMeta(src, False, True),
                    AccountMeta(self.mint, False, False),
                    AccountMeta(dest, False, True),
                    AccountMeta(self.pay_to, True, False),
                ],
            )
        )
        if memo:
            ixs.append(Instruction(MEMO_PROGRAM, memo.encode(), [AccountMeta(self.pay_to, True, False)]))
        blockhash_s, _ = await self._rpc.get_latest_blockhash()
        msg = MessageV0.try_compile(
            self.pay_to, ixs, [], Hash.from_string(blockhash_s),
        )
        tx = VersionedTransaction(msg, [self._kp])
        sig = await self._rpc.send_transaction(base64.b64encode(bytes(tx)).decode("ascii"))
        return sig
