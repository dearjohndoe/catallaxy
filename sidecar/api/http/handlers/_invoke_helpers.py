from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from chains.base import ChainRail, chain_for_rail, namespaced_pub_key, namespaced_tx_key
from payments import PaymentVerificationError, mint_nonce, pub_from_nonce
from settings import AgentSku

from api.http.responses import render_done_response

if TYPE_CHECKING:
    from api.http.handlers.invoke import ParsedInvoke
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")

# Housekeeping TTL for claim_secrets rows — deliberately independent of
# payment_timeout (see build_402_response). Generous enough that a slow
# payer never spuriously loses their claim secret; the sidecar's own
# payment-freshness check (ton_verifier, anchored to on-chain tx time) is
# what actually bounds how long a payment session can be.
CLAIM_SECRET_TTL_SECONDS = 3600


def unlock_quote(quote_id: str | None, sidecar: "SidecarApp") -> None:
    if quote_id and quote_id in sidecar.quotes:
        sidecar.quotes[quote_id].locked = False


def payment_identity_key(rail: str, nonce: str) -> str | None:
    """Refund-queue / claim-block key: ``{chain}:pub:{pub}``.

    ``nonce`` is a full claim nonce or the rebound pub_nonce. Client ``tx``
    is never part of this key.
    """
    pub = pub_from_nonce(nonce)
    if pub is None:
        return None
    return namespaced_pub_key(chain_for_rail(rail), pub)


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
    queue_key = payment_identity_key(parsed.rail, parsed.nonce)
    if queue_key is None:
        # Should be unreachable after split-nonce rebind; never fall back to
        # client tx (that was the refund+goods split).
        logger.error(
            "refund enqueue missing pub identity rail=%s nonce=%s tx=%s",
            parsed.rail, parsed.nonce, parsed.tx_hash,
        )
    try:
        if queue_key is not None:
            await sidecar.refund_queue.enqueue(
                tx_hash=queue_key,
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
    # Rails this SKU can be paid on, each paired with the amount to charge. The
    # health gate and 402 body below are driven off these rail objects.
    priced: list[tuple[ChainRail, int]] = []
    if eff_ton:
        priced.append((sidecar.rails["TON"], min_ton))
    if eff_usd and min_usdt:
        priced.append((sidecar.rails["USDT"], min_usdt))

    unhealthy_rails = [r.rail_id for r, _ in priced if not r.monitor_healthy(max_age)]
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

    # This happens when an SKU uses dynamic pricing and the agent omitted it from
    # `mode=prices` — typically because it's out of stock upstream. Emitting a
    # 402 with empty payment_options makes price-less clients build a payment
    # from undefined address/amount and crash; report out_of_stock instead.
    # Checked before minting a claim secret below so we don't write a row that
    # will never back a usable 402.
    if not priced:
        logger.info(
            "preflight: no purchasable price for sku=%s (dynamic price unresolved) "
            "— reporting out_of_stock", sku.sku_id,
        )
        return web.json_response({"error": "out_of_stock", "sku": sku.sku_id}, status=409)

    # Split-nonce claim auth (TODO-claim-auth.md, PROTOCOL.md §5/§6.1): mint a
    # fresh pub(8 hex)+sec(8 hex) pair, persist a hash of `sec` keyed by `pub`,
    # and advertise only the public half (`pub_nonce`) on-chain via `memo` /
    # the payment cell. `full_nonce` (containing `sec`) is exposed solely in
    # the JSON body's `payment_options[].nonce` field — never in the
    # `x-ton-pay-nonce` header, never in `memo`.
    #
    # TTL is deliberately NOT derived from `payment_timeout`: that setting
    # gates payment *freshness* on a different clock (verify_payment checks
    # `now - tx.now`, anchored to when the on-chain payment confirmed).
    # Anchoring this TTL to mint time instead would falsely reject a buyer
    # who simply took a while to broadcast payment after seeing the 402
    # (wallet app, cross-device QR, etc.) even though their payment is still
    # fresh. This TTL only needs to outlive a realistic "time to pay" window
    # — it's housekeeping so `claim_secrets` doesn't grow unbounded, not a
    # session-length control.
    pub, sec, pub_nonce, full_nonce = mint_nonce(sidecar.sidecar_id)
    try:
        await sidecar.claim_secrets.insert(
            pub, hashlib.sha256(sec.encode()).hexdigest(),
            int(time.time()) + CLAIM_SECRET_TTL_SECONDS,
        )
    except Exception:
        logger.exception("claim_secrets.insert failed — claim will 403 later, refusing 402")
        return web.json_response(
            {"error": "service temporarily unavailable", "retry_after_seconds": 30},
            status=503,
            headers={"Retry-After": "30"},
        )

    payment_options: list[dict[str, Any]] = []
    for rail, amount in priced:
        opt = rail.payment_option(amount, pub_nonce)
        opt["sku"] = sku.sku_id  # not rail-specific; added by the caller
        opt["nonce"] = full_nonce  # full claim value; JSON body only, never on-chain
        payment_options.append(opt)

    resp_body: dict[str, Any] = {
        "error": "Payment required",
        "payment_request": payment_options[0],
        "payment_options": payment_options,
    }

    headers: dict[str, str] = {}
    if eff_ton:
        headers["x-ton-pay-address"] = sidecar.settings.agent_wallet
        headers["x-ton-pay-amount"] = str(min_ton)
        # Public-only half, matching `memo` — NOT the full claim nonce. A
        # header-only client has no way to claim under the split-nonce
        # scheme (it never sees `sec`); see PROTOCOL.md §5.
        headers["x-ton-pay-nonce"] = pub_nonce

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
                    usdt_key = payment_identity_key("USDT", parsed.nonce)
                    if usdt_key is None:
                        logger.error(
                            "USDT refund enqueue missing pub identity nonce=%s tx=%s",
                            parsed.nonce, parsed.tx_hash,
                        )
                    else:
                        await sidecar.refund_queue.enqueue(
                            tx_hash=usdt_key,
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
            return await sidecar.rails["USDT"].verify(
                proof=parsed.tx_hash, nonce=parsed.nonce, min_amount=min_usdt,
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
        return await sidecar.rails["TON"].verify(
            proof=parsed.tx_hash, nonce=parsed.nonce, min_amount=min_ton,
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
            oos_key = payment_identity_key(parsed.rail, parsed.nonce)
            if oos_key is None:
                oos_key = namespaced_tx_key(
                    chain_for_rail(parsed.rail), verified_payment.tx_hash,
                )
            refund_tx = await sidecar.refund_user(
                recipient=verified_payment.sender,
                payment_amount=verified_payment.amount,
                original_tx_hash=oos_key,
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
