from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web

from jetton import USDT_MASTER_MAINNET, USDT_MASTER_TESTNET
from payments import PaymentVerificationError
from settings import AgentSku

from api.http.responses import render_done_response

if TYPE_CHECKING:
    from api.http.handlers.invoke import ParsedInvoke
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


def unlock_quote(quote_id: str | None, sidecar: "SidecarApp") -> None:
    if quote_id and quote_id in sidecar.quotes:
        sidecar.quotes[quote_id].locked = False


async def enqueue_refund_after_payment(
    *,
    sidecar: "SidecarApp",
    parsed: "ParsedInvoke",
    sku: AgentSku,
    sender: str | None,
    amount: int | None,
    reason: str,
    force: bool = False,
) -> web.Response:
    """Enqueue a tx for background refund and return a 503 refund_pending response.

    Used for every post-tx-submission failure where direct refund is unsafe or
    unavailable. The background worker handles retry with backoff. ``force=True``
    bypasses the worker's is_processed race-guard — set it whenever
    ``mark_processed`` has already run but service was NOT delivered.
    """
    unlock_quote(parsed.quote_id, sidecar)
    try:
        await sidecar.refund_queue.enqueue(
            tx_hash=parsed.tx_hash,
            nonce=parsed.nonce,
            rail=parsed.rail,
            sender=sender,
            amount=amount,
            sku_id=sku.sku_id,
            force_refund=force,
        )
    except Exception:
        # Last resort: queue itself unavailable. Log loudly — ops must reconcile
        # manually. We still return refund_pending so the caller doesn't retry
        # the /invoke and burn more state.
        logger.exception(
            "refund_queue.enqueue failed tx=%s nonce=%s — manual reconciliation needed",
            parsed.tx_hash, parsed.nonce,
        )
    if sidecar.owner_bot is not None:
        sidecar.owner_bot.notify_refund(
            sender=sender, amount=amount, rail=parsed.rail, sku_id=sku.sku_id,
            tx_hash=parsed.tx_hash, reason=reason, refund_tx=None,
            status="refund_pending",
        )
    return web.json_response(
        {
            "error": f"Internal sidecar error ({reason}); payment queued for refund",
            "refund_pending": True,
            "tx": parsed.tx_hash,
        },
        status=503,
    )


async def build_402_response(
    parsed: "ParsedInvoke",
    sku: AgentSku,
    sidecar: "SidecarApp",
    eff_ton: int,
    eff_usd: int,
    min_ton: int,
    min_usdt: int,
) -> web.Response:
    """Preflight response: stock gate + monitor health gate + 402 Payment Required."""
    view = await sidecar.stock.get_view(sku.sku_id)
    if view.stock_left is not None and view.stock_left <= 0:
        return web.json_response({"error": "out_of_stock", "sku": sku.sku_id}, status=409)

    # Monitor-health gate (plan D). If a rail we would advertise has no fresh
    # successful poll, refuse the preflight with 503 — better than taking the
    # payment when we can't detect it. Callers retry after `Retry-After`.
    try:
        max_age = float(os.environ.get("PAYMENT_MONITOR_MAX_AGE_SEC", "60"))
    except ValueError:
        max_age = 60.0
    unhealthy_rails: list[str] = []
    if eff_ton and (sidecar.verifier is None or not sidecar.verifier.is_healthy(max_age)):
        unhealthy_rails.append("TON")
    if eff_usd and min_usdt and (
        sidecar.jetton_verifier is None or not sidecar.jetton_verifier.is_healthy(max_age)
    ):
        unhealthy_rails.append("USDT")
    if unhealthy_rails:
        logger.warning(
            "preflight refused: payment monitor degraded for rails=%s sku=%s",
            unhealthy_rails, sku.sku_id,
        )
        return web.json_response(
            {
                "error": "service temporarily unavailable",
                "detail": f"payment monitor degraded ({', '.join(unhealthy_rails)})",
                "retry_after_seconds": 60,
            },
            status=503,
            headers={"Retry-After": "60"},
        )

    nonce = parsed.nonce
    if not nonce or not nonce.endswith(f":{sidecar.sidecar_id}"):
        nonce = f"{uuid.uuid4().hex[:16]}:{sidecar.sidecar_id}"

    payment_options: list[dict[str, Any]] = []
    if eff_ton:
        payment_options.append({
            "rail": "TON",
            "address": sidecar.settings.agent_wallet,
            "amount": str(min_ton),
            "memo": nonce,
            "sku": sku.sku_id,
        })
    if eff_usd and min_usdt:
        usdt_master = USDT_MASTER_TESTNET if sidecar.settings.testnet else USDT_MASTER_MAINNET
        payment_options.append({
            "rail": "USDT",
            "address": sidecar.settings.agent_wallet,
            "amount": str(min_usdt),
            "memo": nonce,
            "sku": sku.sku_id,
            "token": {"symbol": "USDT", "master": usdt_master, "decimals": 6},
        })

    # This happens when an SKU uses dynamic pricing and the agent omitted it from
    # `mode=prices` — typically because it's out of stock upstream. Emitting a
    # 402 with empty payment_options makes price-less clients build a payment
    # from undefined address/amount and crash; report out_of_stock instead.
    if not payment_options:
        logger.info(
            "preflight: no purchasable price for sku=%s (dynamic price unresolved) "
            "— reporting out_of_stock", sku.sku_id,
        )
        return web.json_response({"error": "out_of_stock", "sku": sku.sku_id}, status=409)

    resp_body: dict[str, Any] = {
        "error": "Payment required",
        "payment_request": payment_options[0] if payment_options else {},
        "payment_options": payment_options,
    }

    headers: dict[str, str] = {}
    if eff_ton:
        headers["x-ton-pay-address"] = sidecar.settings.agent_wallet
        headers["x-ton-pay-amount"] = str(min_ton)
        headers["x-ton-pay-nonce"] = nonce

    return web.json_response(resp_body, status=402, headers=headers)


async def verify_payment(
    parsed: "ParsedInvoke",
    sku: AgentSku,
    sidecar: "SidecarApp",
    min_ton: int,
    min_usdt: int,
) -> Any | web.Response:
    """Run the right verifier (TON or USDT). Unlocks the quote on every error path."""
    try:
        if parsed.rail == "USDT":
            if not sidecar.jetton_verifier or not sidecar._agent_jetton_wallet:
                # Try to bootstrap on the fly — covers both startup misconfig
                # (verifier never created) and transient liteserver outage
                # (start() failed at boot).
                bootstrapped = await sidecar.ensure_jetton_verifier()
                if not bootstrapped:
                    await sidecar.refund_queue.enqueue(
                        tx_hash=parsed.tx_hash,
                        nonce=parsed.nonce,
                        rail="USDT",
                        sku_id=sku.sku_id,
                    )
                    unlock_quote(parsed.quote_id, sidecar)
                    logger.warning(
                        "USDT payment received but jetton_verifier unavailable — "
                        "queued for background refund tx=%s nonce=%s",
                        parsed.tx_hash, parsed.nonce,
                    )
                    if sidecar.owner_bot is not None:
                        sidecar.owner_bot.notify_refund(
                            sender=None, amount=None, rail="USDT", sku_id=sku.sku_id,
                            tx_hash=parsed.tx_hash, reason="usdt_verifier_unavailable",
                            refund_tx=None, status="refund_pending",
                        )
                    return web.json_response(
                        {
                            "error": "USDT verifier temporarily unavailable; payment queued for refund",
                            "refund_pending": True,
                            "tx": parsed.tx_hash,
                        },
                        status=503,
                    )
            if min_usdt == 0:
                # Dynamic-price SKU and price fetch failed. The user already
                # submitted a tx, so we don't know if it's real until the
                # worker recovers sender/amount from the monitor.
                logger.warning(
                    "USDT price unavailable for SKU %s — queueing tx %s for refund",
                    sku.sku_id, parsed.tx_hash,
                )
                return await enqueue_refund_after_payment(
                    sidecar=sidecar, parsed=parsed, sku=sku,
                    sender=None, amount=None,
                    reason="usdt_price_unavailable",
                )
            return await sidecar.jetton_verifier.verify(
                tx_hash=parsed.tx_hash, raw_nonce=parsed.nonce, min_amount=min_usdt,
            )
        if min_ton == 0:
            logger.warning(
                "TON price unavailable for SKU %s — queueing tx %s for refund",
                sku.sku_id, parsed.tx_hash,
            )
            return await enqueue_refund_after_payment(
                sidecar=sidecar, parsed=parsed, sku=sku,
                sender=None, amount=None,
                reason="ton_price_unavailable",
            )
        return await sidecar.verifier.verify(
            tx_hash=parsed.tx_hash, raw_nonce=parsed.nonce, min_amount=min_ton,
        )
    except PaymentVerificationError as exc:
        # Verifier saw on-chain state and rejected: tx not found, wrong amount,
        # wrong recipient. Money may not exist — don't refund, let user fix.
        unlock_quote(parsed.quote_id, sidecar)
        return web.json_response({"error": str(exc)}, status=402)
    except Exception:
        # Unknown verifier error (RPC blip, parsing bug, etc.). We can't tell
        # whether the tx is real. Worker's _recover_payment_info will check
        # the monitor and either refund or mark failed.
        logger.exception("Payment verification error tx=%s", parsed.tx_hash)
        return await enqueue_refund_after_payment(
            sidecar=sidecar, parsed=parsed, sku=sku,
            sender=None, amount=None,
            reason="verifier_error",
        )


async def claim_stock(
    parsed: "ParsedInvoke",
    sku: AgentSku,
    sidecar: "SidecarApp",
    verified_payment: Any,
) -> tuple[str | None, list[str], web.Response | None]:
    """Reserve stock for direct calls (quote calls already reserved at quote time).

    Returns (reservation_key, created_keys, error_response).
    """
    created: list[str] = []
    if parsed.quote_id:
        return parsed.quote_id, created, None
    if not sidecar.stock.has_tracked_stock(sku.sku_id):
        return None, created, None

    reservation_key = verified_payment.tx_hash
    try:
        reserved = await sidecar.stock.reserve(
            sku.sku_id, reservation_key, sidecar.settings.final_timeout,
        )
    except Exception:
        logger.exception("stock.reserve (post-payment) failed")
        reserved = False
    if not reserved:
        # Race lost between preflight and payment. Refund the user.
        # ``mark_processed`` already ran by the time we get here, so any
        # fallback enqueue must set force_refund=True (otherwise the worker's
        # is_processed race-guard would skip it).
        refund_tx: str | None = None
        refund_send_failed = False
        try:
            refund_tx = await sidecar.refund_user(
                recipient=verified_payment.sender,
                payment_amount=verified_payment.amount,
                original_tx_hash=verified_payment.tx_hash,
                reason="out_of_stock",
                rail=parsed.rail,
            )
        except Exception:
            logger.exception("Refund after out_of_stock race failed")
            refund_send_failed = True

        if refund_tx:
            if sidecar.owner_bot is not None:
                sidecar.owner_bot.notify_refund(
                    sender=verified_payment.sender, amount=verified_payment.amount,
                    rail=parsed.rail, sku_id=sku.sku_id,
                    tx_hash=verified_payment.tx_hash, reason="out_of_stock",
                    refund_tx=refund_tx, status="refunded",
                )
            return None, created, web.json_response(
                {"error": "out_of_stock", "sku": sku.sku_id,
                 "refunded": True, "refund_tx": refund_tx},
                status=409,
            )

        # Direct send returned None or raised. Queue for the worker to retry.
        logger.warning(
            "Direct refund failed after OOS race tx=%s; queueing for background retry "
            "(send_failed=%s)",
            verified_payment.tx_hash, refund_send_failed,
        )
        return None, created, await enqueue_refund_after_payment(
            sidecar=sidecar, parsed=parsed, sku=sku,
            sender=verified_payment.sender, amount=verified_payment.amount,
            reason="out_of_stock_refund_send_failed",
            force=True,
        )
    created.append(reservation_key)
    return reservation_key, created, None


def build_agent_payload(parsed: "ParsedInvoke", sku: AgentSku) -> dict[str, Any]:
    agent_body = dict(parsed.body)
    agent_body["sku"] = sku.sku_id
    for field_name, file_path in parsed.uploaded_files.items():
        agent_body[f"{field_name}_path"] = str(file_path)
        if f"{field_name}_name" not in agent_body:
            agent_body[f"{field_name}_name"] = file_path.name
    return {
        "capability": parsed.capability,
        "sku": sku.sku_id,
        "body": agent_body,
    }


async def wait_and_render(job_id: str, sidecar: "SidecarApp") -> web.Response:
    record = await sidecar.jobs.wait_for_completion(job_id, timeout_seconds=sidecar.settings.sync_timeout)
    if record is None:
        return web.json_response({"job_id": job_id, "status": "pending"})
    if record.status == "done":
        return render_done_response(
            job_id, record.result,
            sidecar._file_store, sidecar._file_store_dir, sidecar._file_store_ttl,
        )
    if record.status == "error":
        return web.json_response({"job_id": job_id, "status": "error", "error": record.error}, status=500)
    return web.json_response({"job_id": job_id, "status": "pending"})
