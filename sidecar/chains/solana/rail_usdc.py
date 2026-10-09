"""USDC-on-Solana rail: x402 exact, we settle as fee payer."""

from __future__ import annotations

import logging
import struct
from typing import Any

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
    TOKEN_PROGRAM,
    USDC_MINT_DEVNET,
    USDC_MINT_MAINNET,
)
from chains.solana.exact import ExactSettler, InspectedExactPayment, inspect_exact
from chains.solana.rpc import SolanaRpc
from chains.solana.x402 import cluster_caip2, exact_requirements
from payments.types import VerifiedPayment

logger = logging.getLogger("sidecar")


class UsdcRail:
    rail_id = "USDC"

    def __init__(
        self,
        *,
        keypair: Keypair,
        rpc: SolanaRpc,
        cluster: str,
        mint: str | None = None,
        settler: ExactSettler | None = None,
    ) -> None:
        self._kp = keypair
        self._rpc = rpc
        self.cluster = "devnet" if cluster == "devnet" else "mainnet"
        self.mint = Pubkey.from_string(
            mint
            or (USDC_MINT_DEVNET if self.cluster == "devnet" else USDC_MINT_MAINNET)
        )
        self.pay_to = keypair.pubkey()
        self._settler = settler or ExactSettler(keypair=keypair, rpc=rpc)

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
        if amount <= 0:
            logger.warning("USDC refund skipped: amount <= 0 tx=%s", original_tx_hash)
            return None
        try:
            return await self._send_usdc(to, amount)
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
        # No watcher: we publish the tx ourselves. Unreachable RPC fails at settle.
        return True

    async def consume(self, nonce: str) -> None:
        return None

    async def close(self) -> None:
        await self._rpc.close()

    async def _send_usdc(self, owner_b58: str, amount: int) -> str:
        dest_owner = Pubkey.from_string(owner_b58)
        src = get_associated_token_address(self.pay_to, self.mint, TOKEN_PROGRAM)
        dest = get_associated_token_address(dest_owner, self.mint, TOKEN_PROGRAM)
        ixs: list[Instruction] = [
            set_compute_unit_limit(50_000),
            set_compute_unit_price(1),
        ]
        dest_info = await self._rpc.get_account_info(str(dest))
        if dest_info is None:
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
        blockhash_s, _ = await self._rpc.get_latest_blockhash()
        msg = MessageV0.try_compile(
            self.pay_to, ixs, [], Hash.from_string(blockhash_s),
        )
        tx = VersionedTransaction(msg, [self._kp])
        import base64
        sig = await self._rpc.send_transaction(base64.b64encode(bytes(tx)).decode("ascii"))
        return sig
