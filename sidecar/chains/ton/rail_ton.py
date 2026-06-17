"""TON native rail — ChainRail adapter over the existing TON payment engine.

Step 3 of the multichain refactor (MULTICHAIN_PLAN.md). This wraps the pieces
that today are scattered across ``PaymentVerifier``, ``_invoke_helpers`` and
``api.domain.refund.refund_user`` into one object that satisfies
``chains.base.ChainRail``. It is NOT yet wired into the handlers — step 4
rewires ``build_402_response``/``verify_payment`` to iterate rail objects. Until
then this is exercised only by unit tests.

Behaviour is bit-for-bit with the current TON paths:
- ``refund`` is the exact TON branch of ``refund_user`` (the per-rail dispatch
  that the refactor eliminates — each rail now owns only its own refund).
- ``payment_option`` is the TON dict built in ``build_402_response``.
- ``rail_id``/the ``"rail"`` wire value stay ``"TON"``; migrating to the
  canonical lowercase scheme ("ton", "ton:usdt") is protocol-v2 work (plan §4).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from chains.ton.transfer import TransferSender, refund_body
from payments.types import VerifiedPayment

logger = logging.getLogger("sidecar")


class TonRail:
    rail_id = "TON"

    def __init__(
        self,
        *,
        get_verifier: Callable[[], Any | None],
        sender: TransferSender,
        agent_wallet: str,
        sidecar_id: str,
        refund_fee_nanoton: int,
    ) -> None:
        # ``get_verifier`` is a callable, not a snapshot: the PaymentVerifier is
        # created/started in lifecycle and its internal client can be rebuilt,
        # so we read the live reference on each call.
        self._get_verifier = get_verifier
        self._sender = sender
        self._agent_wallet = agent_wallet
        self._sidecar_id = sidecar_id
        self._refund_fee_nanoton = refund_fee_nanoton

    async def verify(self, proof: str, nonce: str, min_amount: int) -> VerifiedPayment:
        verifier = self._get_verifier()
        if verifier is None:
            raise RuntimeError("TON verifier not started")
        return await verifier.verify(tx_hash=proof, raw_nonce=nonce, min_amount=min_amount)

    async def refund(
        self, to: str, amount: int, *, original_tx_hash: str, reason: str,
    ) -> str | None:
        refund_amount = max(amount - self._refund_fee_nanoton, 0)
        if refund_amount <= 0:
            logger.warning(
                "Refund skipped because amount is not enough after fee",
                extra={
                    "tx_hash": original_tx_hash,
                    "payment_amount": amount,
                    "refund_fee": self._refund_fee_nanoton,
                },
            )
            return None
        try:
            return await self._sender.send(
                to, refund_amount, refund_body(original_tx_hash, reason, self._sidecar_id),
            )
        except Exception:
            logger.exception("Failed to send refund")
            return None

    def payment_option(self, amount: int, nonce: str) -> dict[str, Any]:
        return {
            "rail": "TON",
            "address": self._agent_wallet,
            "amount": str(amount),
            "memo": nonce,
        }

    def monitor_healthy(self, max_age_seconds: float = 60.0) -> bool:
        verifier = self._get_verifier()
        return bool(verifier is not None and verifier.is_healthy(max_age_seconds))
