from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import api  # late binding for monkeypatched run_agent_subprocess
from api.domain.refund import refund_or_enqueue
from api.domain.result_processing import is_out_of_stock_result
from api.validation import validate_result_structure

if TYPE_CHECKING:
    from owner_bot import OwnerBot
    from payments.refund_queue import RefundQueue

logger = logging.getLogger("sidecar")


def _exc_to_reason_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValueError):
        return "invalid_response"
    if isinstance(exc, RuntimeError):
        return "execution_failed"
    return "internal_error"


def create_runner(
    *,
    refund_user: Callable[..., Awaitable[str | None]],
    refund_queue: "RefundQueue",
    stock,
    agent_command: str,
    final_timeout: int,
    sidecar_id: str,
    agent_payload: dict[str, Any],
    sender: str,
    amount: int,
    tx_hash: str,
    nonce: str,
    sku_id: str,
    uploaded_files: dict[str, Path] | None = None,
    rail: str = "TON",
    reservation_key: str | None = None,
    owner_bot: "OwnerBot | None" = None,
    user_body: Any = None,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def runner() -> dict[str, Any]:
        try:
            raw = await api.run_agent_subprocess(
                command=agent_command,
                payload=agent_payload,
                timeout_seconds=final_timeout,
                env={
                    "OWN_SIDECAR_ID": sidecar_id,
                    "CALLER_ADDRESS": sender,
                    "CALLER_TX_HASH": tx_hash,
                    "PAYMENT_RAIL": rail,
                },
            )

            if is_out_of_stock_result(raw):
                reason = str(raw.get("reason") or "agent reported out of stock")
                refund_tx = await refund_or_enqueue(
                    refund_queue=refund_queue,
                    refund_user_fn=refund_user,
                    tx_hash=tx_hash, nonce=nonce, rail=rail,
                    sender=sender, amount=amount, sku_id=sku_id,
                    reason="out_of_stock",
                )
                if reservation_key:
                    try:
                        await stock.agent_out_of_stock(reservation_key)
                    except Exception:
                        logger.exception("agent_out_of_stock bookkeeping failed")
                if owner_bot is not None:
                    owner_bot.notify_refund(
                        sender=sender, amount=amount, rail=rail, sku_id=sku_id,
                        tx_hash=tx_hash, reason=f"out_of_stock: {reason}",
                        refund_tx=refund_tx,
                        status="refunded" if refund_tx else "refund_pending",
                    )
                return {
                    "result": {
                        "status": "refunded" if refund_tx else "refund_pending",
                        "reason_code": "out_of_stock",
                        "reason": reason,
                        "refund_tx": refund_tx,
                    }
                }

            validate_result_structure(raw)
            if reservation_key:
                try:
                    await stock.commit_sold(reservation_key, tx_hash)
                except Exception:
                    logger.exception("commit_sold failed (agent succeeded but stock bookkeeping broke)")
            if owner_bot is not None:
                owner_bot.notify_success(
                    sender=sender, amount=amount, rail=rail, sku_id=sku_id,
                    tx_hash=tx_hash, body=user_body,
                )
            return raw
        except Exception as exc:
            reason_code = _exc_to_reason_code(exc)
            human_reason = str(exc) or reason_code

            refund_tx = await refund_or_enqueue(
                refund_queue=refund_queue,
                refund_user_fn=refund_user,
                tx_hash=tx_hash, nonce=nonce, rail=rail,
                sender=sender, amount=amount, sku_id=sku_id,
                reason=reason_code,
            )
            if reservation_key:
                try:
                    await stock.release(reservation_key)
                except Exception:
                    logger.exception("stock.release failed inside runner")

            if owner_bot is not None:
                owner_bot.notify_refund(
                    sender=sender, amount=amount, rail=rail, sku_id=sku_id,
                    tx_hash=tx_hash, reason=f"{reason_code}: {human_reason}",
                    refund_tx=refund_tx,
                    status="refunded" if refund_tx else "refund_pending",
                )
            if refund_tx:
                return {
                    "result": {
                        "status": "refunded",
                        "reason_code": reason_code,
                        "reason": human_reason,
                        "refund_tx": refund_tx,
                    }
                }
            # Direct refund failed, but the queue has it now (force_refund=True).
            # Return a deterministic refund_pending result instead of raising —
            # raising would make jobs mark this 'error', hiding the queued refund
            # from the caller.
            return {
                "result": {
                    "status": "refund_pending",
                    "reason_code": reason_code,
                    "reason": human_reason,
                    "refund_tx": None,
                }
            }
        finally:
            if uploaded_files:
                for file_path in uploaded_files.values():
                    try:
                        shutil.rmtree(file_path.parent, ignore_errors=True)
                    except Exception:
                        logger.warning("Failed to cleanup uploaded file dir %s", file_path.parent)
    return runner
