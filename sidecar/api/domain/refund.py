from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from jetton import JETTON_TRANSFER_OPCODE, USDT_REFUND_FEE
from transfer import REFUND_OPCODE, TransferSender, refund_body

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


def _decode_refund_comment(body: Any) -> dict[str, Any] | None:
    """Decode an outgoing-msg body shaped like ``refund_body``: REFUND_OPCODE
    + snake-string JSON. Returns the parsed dict or None if the body is not
    a refund comment."""
    if body is None:
        return None
    try:
        s = body.begin_parse()
        if s.remaining_bits < 32:
            return None
        if s.load_uint(32) != REFUND_OPCODE:
            return None
        return json.loads(s.load_snake_string())
    except Exception:
        return None


def _decode_jetton_forward_refund(body: Any) -> dict[str, Any] | None:
    """Decode a jetton_transfer body whose forward_payload is a refund_body.
    Returns the parsed refund JSON or None."""
    if body is None:
        return None
    try:
        s = body.begin_parse()
        if s.remaining_bits < 32 or s.load_uint(32) != JETTON_TRANSFER_OPCODE:
            return None
        s.load_uint(64)     # query_id
        s.load_coins()      # amount
        s.load_address()    # destination
        s.load_address()    # response_destination
        if s.load_bit():    # custom_payload presence bit
            if s.remaining_refs == 0:
                return None
            s.load_ref()
        s.load_coins()      # forward_ton_amount
        if s.remaining_bits == 0:
            return None
        if s.load_bit():
            if s.remaining_refs == 0:
                return None
            fp = s.load_ref()
        else:
            from pytoniq_core import begin_cell
            b = begin_cell()
            b.store_slice(s)
            fp = b.end_cell()
        return _decode_refund_comment(fp)
    except Exception:
        return None


async def find_existing_refund_tx(
    *,
    client,
    agent_wallet: str,
    rail: str,
    original_tx_hash: str,
    sidecar_id: str,
    limit: int = 50,
) -> str | None:
    """Scan agent's recent outgoing messages for a refund matching the
    fingerprint (original_tx_hash, sidecar_id). Returns the on-chain hash
    of the refund transaction if found, otherwise None.

    The fingerprint is unique per refund — refund_body stamps both fields —
    so a match means a previous worker attempt already delivered the refund.
    Use this to avoid double-sending after a crash between TransferSender.send()
    and refund_queue.mark_refunded().

    For both rails, the relevant outgoing message originates at ``agent_wallet``:
    TON refunds carry the refund_body directly; USDT refunds wrap it in the
    jetton_transfer forward_payload of the message going to agent's jetton
    wallet.
    """
    try:
        txs = await client.get_transactions(agent_wallet, limit=limit)
    except Exception:
        logger.exception(
            "find_existing_refund_tx: get_transactions failed tx=%s rail=%s",
            original_tx_hash, rail,
        )
        return None

    target = (original_tx_hash, sidecar_id)
    for tx in txs:
        if not tx.out_msgs:
            continue
        for msg in tx.out_msgs:
            if rail == "USDT":
                payload = _decode_jetton_forward_refund(msg.body)
            else:
                payload = _decode_refund_comment(msg.body)
            if payload is None:
                continue
            if (payload.get("tx"), payload.get("sidecar_id")) == target:
                try:
                    return tx.cell.hash.hex()
                except Exception:
                    logger.warning(
                        "find_existing_refund_tx: matched body but cell hash unreadable tx=%s",
                        original_tx_hash,
                    )
                    return None
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
