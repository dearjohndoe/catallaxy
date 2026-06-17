"""USDT-on-TON rail — ChainRail adapter over the jetton payment engine.

Companion to ``rail_ton.TonRail`` (see that module's header). Wraps
``JettonPaymentVerifier`` + the USDT branch of ``refund_user`` +
``build_402_response``'s USDT option into a ``chains.base.ChainRail``. Not yet
wired into handlers (step 4). Behaviour is bit-for-bit with today's USDT paths.

The agent's jetton wallet is bootstrapped lazily (``ensure_jetton_verifier``),
so both the verifier and the wallet address are read through callables rather
than captured at construction.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from chains.ton.jetton import USDT_REFUND_FEE
from chains.ton.transfer import TransferSender, refund_body
from payments.types import VerifiedPayment

logger = logging.getLogger("sidecar")


class UsdtRail:
    rail_id = "USDT"

    def __init__(
        self,
        *,
        get_verifier: Callable[[], Any | None],
        get_agent_jetton_wallet: Callable[[], str | None],
        sender: TransferSender,
        agent_wallet: str,
        usdt_master: str,
        sidecar_id: str,
    ) -> None:
        self._get_verifier = get_verifier
        self._get_agent_jetton_wallet = get_agent_jetton_wallet
        self._sender = sender
        self._agent_wallet = agent_wallet
        self._usdt_master = usdt_master
        self._sidecar_id = sidecar_id

    async def verify(self, proof: str, nonce: str, min_amount: int) -> VerifiedPayment:
        verifier = self._get_verifier()
        if verifier is None:
            raise RuntimeError("USDT verifier not started")
        return await verifier.verify(tx_hash=proof, raw_nonce=nonce, min_amount=min_amount)

    async def refund(
        self, to: str, amount: int, *, original_tx_hash: str, reason: str,
    ) -> str | None:
        refund_amount = max(amount - USDT_REFUND_FEE, 0)
        if refund_amount <= 0:
            logger.warning(
                "USDT refund skipped: amount too small after fee",
                extra={"tx_hash": original_tx_hash, "payment_amount": amount},
            )
            return None
        try:
            fwd = refund_body(original_tx_hash, reason, self._sidecar_id)
            return await self._sender.send_jetton(
                own_jetton_wallet=self._get_agent_jetton_wallet() or "",
                destination=to,
                jetton_amount=refund_amount,
                forward_payload=fwd,
            )
        except Exception:
            logger.exception("Failed to send USDT refund")
            return None

    def payment_option(self, amount: int, nonce: str) -> dict[str, Any]:
        return {
            "rail": "USDT",
            "address": self._agent_wallet,
            "amount": str(amount),
            "memo": nonce,
            "token": {"symbol": "USDT", "master": self._usdt_master, "decimals": 6},
        }

    def monitor_healthy(self, max_age_seconds: float = 60.0) -> bool:
        verifier = self._get_verifier()
        return bool(verifier is not None and verifier.is_healthy(max_age_seconds))
