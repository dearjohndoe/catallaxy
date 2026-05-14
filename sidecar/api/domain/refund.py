from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jetton import USDT_REFUND_FEE
from transfer import TransferSender, refund_body

if TYPE_CHECKING:
    from payments.refund_queue import RefundQueue

logger = logging.getLogger("sidecar")


async def refund_user(
    *,
    sender: TransferSender,
    agent_jetton_wallet: str | None,
    sidecar_id: str,
    refund_fee_nanoton: int,
    recipient: str,
    payment_amount: int,
    original_tx_hash: str,
    reason: str,
    rail: str = "TON",
) -> str | None:
    """Send refund back to `recipient`. Returns refund tx hash on success, None otherwise."""
    if rail == "USDT":
        refund_amount = max(payment_amount - USDT_REFUND_FEE, 0)
        if refund_amount <= 0:
            logger.warning(
                "USDT refund skipped: amount too small after fee",
                extra={"tx_hash": original_tx_hash, "payment_amount": payment_amount},
            )
            return None
        try:
            fwd = refund_body(original_tx_hash, reason, sidecar_id)
            return await sender.send_jetton(
                own_jetton_wallet=agent_jetton_wallet or "",
                destination=recipient,
                jetton_amount=refund_amount,
                forward_payload=fwd,
            )
        except Exception:
            logger.exception("Failed to send USDT refund")
            return None

    refund_amount = max(payment_amount - refund_fee_nanoton, 0)
    if refund_amount <= 0:
        logger.warning(
            "Refund skipped because amount is not enough after fee",
            extra={
                "tx_hash": original_tx_hash,
                "payment_amount": payment_amount,
                "refund_fee": refund_fee_nanoton,
            },
        )
        return None

    try:
        return await sender.send(recipient, refund_amount, refund_body(original_tx_hash, reason, sidecar_id))
    except Exception:
        logger.exception("Failed to send refund")
        return None


async def refund_or_enqueue(
    *,
    refund_queue: "RefundQueue",
    refund_user_fn,
    tx_hash: str,
    nonce: str,
    rail: str,
    sender: str,
    amount: int,
    sku_id: str | None,
    reason: str,
) -> str | None:
    """Direct refund attempt; on failure (None or exception), enqueue with force=True.

    Used by paths where mark_processed already ran but service wasn't delivered
    (runner failure, agent reported out_of_stock). The queue entry must bypass
    the worker's is_processed race-guard, hence force_refund=True.
    """
    try:
        refund_tx = await refund_user_fn(
            recipient=sender,
            payment_amount=amount,
            original_tx_hash=tx_hash,
            reason=reason,
            rail=rail,
        )
    except Exception:
        logger.exception("Direct refund raised tx=%s; falling back to queue", tx_hash)
        refund_tx = None

    if refund_tx:
        return refund_tx

    try:
        await refund_queue.enqueue(
            tx_hash=tx_hash, nonce=nonce, rail=rail,
            sender=sender, amount=amount, sku_id=sku_id,
            force_refund=True,
        )
    except Exception:
        logger.exception(
            "refund_queue.enqueue failed tx=%s — manual reconciliation needed", tx_hash,
        )
    return None
