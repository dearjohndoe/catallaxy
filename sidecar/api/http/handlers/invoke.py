from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from aiohttp import web

from payments import parse_nonce
from settings import AgentSku

from api.domain.invocation import create_runner
from api.domain.pricing import resolve_sku
from api.domain.quoting import QuoteEntry, cleanup_expired_quotes, fetch_dynamic_prices
from api.http.multipart import parse_multipart_invoke
from api.infra.files import cleanup_uploaded_files
from api.validation import validate_body
from api.http.handlers._invoke_helpers import (
    build_402_response,
    build_agent_payload,
    claim_stock,
    enqueue_refund_after_payment,
    unlock_quote,
    verify_payment,
    wait_and_render,
)

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


@dataclass
class ParsedInvoke:
    tx_hash: str
    nonce: str
    capability: str
    quote_id: str | None
    rail: str
    sku_field: str | None
    body: dict[str, Any]
    payload: dict[str, Any]  # full request payload (for validate_body)
    uploaded_files: dict[str, Path] = field(default_factory=dict)


async def _parse_invoke_request(
    request: web.Request, file_store_dir: Path
) -> ParsedInvoke | web.Response:
    try:
        if request.content_type and "multipart/form-data" in request.content_type:
            tx_hash, nonce, capability, quote_id, rail, sku_field, body, uploaded_files = \
                await parse_multipart_invoke(request, file_store_dir)
            return ParsedInvoke(
                tx_hash=tx_hash, nonce=nonce, capability=capability,
                quote_id=quote_id, rail=rail, sku_field=sku_field,
                body=body, payload={"body": body}, uploaded_files=uploaded_files,
            )
        data = await request.json()
        return ParsedInvoke(
            tx_hash=str(data.get("tx", "")).strip(),
            nonce=str(data.get("nonce", "")).strip(),
            capability=str(data.get("capability", "")).strip(),
            quote_id=str(data.get("quote_id", "")).strip() or None,
            rail=str(data.get("rail", "")).strip().upper() or "TON",
            sku_field=str(data.get("sku", "")).strip() or None,
            body=data.get("body", {}),
            payload=data,
        )
    except Exception:
        return web.json_response({"error": "Invalid request"}, status=400)


def _resolve_sku_for_invoke(
    parsed: ParsedInvoke, sidecar: "SidecarApp"
) -> tuple[AgentSku | None, web.Response | None]:
    """Quote-bound calls derive SKU from the quote; direct calls use sku field."""
    quote_entry = sidecar.quotes.get(parsed.quote_id) if parsed.quote_id else None
    if quote_entry is not None:
        sku = sidecar._skus_by_id.get(quote_entry.sku_id)
        if sku is None:
            return None, web.json_response({"error": "Quote references unknown SKU"}, status=500)
        return sku, None
    return resolve_sku(parsed.sku_field, sidecar._skus_by_id, sidecar._single_sku, sidecar.settings.skus)


async def _resolve_amounts(sku: AgentSku, sidecar: "SidecarApp") -> tuple[int, int, int, int]:
    """Return (eff_ton, eff_usd, min_ton, min_usdt). Dynamic SKUs hit the agent."""
    eff_ton = sku.price_ton or 0
    eff_usd = sku.price_usd or 0
    if sku.price_ton == 0 and sku.price_usd == 0:
        try:
            prices = await fetch_dynamic_prices(
                sidecar._dynamic_prices_cache,
                agent_command=sidecar.settings.agent_command,
                sync_timeout=sidecar.settings.sync_timeout,
                sidecar_id=sidecar.sidecar_id,
            )
            dp = prices.get(sku.sku_id, {})
            eff_ton = dp.get("ton") or 0
            eff_usd = dp.get("usd") or 0
        except Exception:
            logger.warning("Dynamic price fetch failed for SKU %s", sku.sku_id)
    return eff_ton, eff_usd, eff_ton or 0, eff_usd or 0


def _apply_quote_amounts(
    parsed: ParsedInvoke, sidecar: "SidecarApp", sku: AgentSku, min_ton: int, min_usdt: int,
) -> tuple[QuoteEntry | None, int, int, web.Response | None]:
    """Override amounts from a quote entry; report quote errors.

    The quote override is rail-aware: ``quote_entry.price`` is only trusted as
    ``min_ton`` when the SKU actually prices a TON rail, and ``price_usdt`` only
    when it prices a USDT rail. Without this guard a USD-only SKU could leak
    micro-USD into ``min_ton`` and let TON payment pass at ~1% of the price.
    """
    if not parsed.quote_id:
        return None, min_ton, min_usdt, None
    cleanup_expired_quotes(sidecar.quotes)
    quote_entry = sidecar.quotes.get(parsed.quote_id)
    if quote_entry is None:
        return None, min_ton, min_usdt, web.json_response(
            {"error": "Quote not found or expired"}, status=400,
        )
    if quote_entry.locked and parsed.tx_hash:
        return None, min_ton, min_usdt, web.json_response(
            {"error": "Quote is currently locked by another request"}, status=409,
        )
    new_min_ton = quote_entry.price if sku.price_ton is not None else min_ton
    new_min_usdt = (quote_entry.price_usdt or min_usdt) if sku.price_usd is not None else min_usdt
    return quote_entry, new_min_ton, new_min_usdt, None


async def handle_invoke(request: web.Request, sidecar: "SidecarApp") -> web.Response:
    parsed = await _parse_invoke_request(request, sidecar._file_store_dir)
    if isinstance(parsed, web.Response):
        return parsed

    uploaded_files = parsed.uploaded_files
    ownership_transferred = False
    created_reservation_keys: list[str] = []

    try:
        if not parsed.capability:
            return web.json_response({"error": "capability is required"}, status=400)
        if parsed.capability != sidecar.settings.capability:
            return web.json_response({"error": "Unsupported capability"}, status=400)

        sku, sku_err = _resolve_sku_for_invoke(parsed, sidecar)
        if sku_err is not None:
            return sku_err
        assert sku is not None

        if parsed.rail == "TON" and sku.price_ton is None:
            return web.json_response(
                {"error": "unsupported_rail_for_sku", "sku": sku.sku_id, "rail": parsed.rail}, status=400,
            )
        if parsed.rail == "USDT" and sku.price_usd is None:
            return web.json_response(
                {"error": "unsupported_rail_for_sku", "sku": sku.sku_id, "rail": parsed.rail}, status=400,
            )

        eff_ton, eff_usd, min_ton, min_usdt = await _resolve_amounts(sku, sidecar)
        quote_entry, min_ton, min_usdt, quote_err = _apply_quote_amounts(parsed, sidecar, sku, min_ton, min_usdt)
        if quote_err is not None:
            return quote_err

        if not parsed.tx_hash:
            return await build_402_response(parsed, sku, sidecar, eff_ton, eff_usd, min_ton, min_usdt)

        missing = validate_body(parsed.payload, sidecar.args_schema, has_tx=True, uploaded_files=uploaded_files)
        if missing:
            return web.json_response({"error": "Missing required fields", "missing": missing}, status=400)
        if not parsed.nonce:
            return web.json_response({"error": "nonce is required with tx"}, status=400)

        if parsed.quote_id and quote_entry:
            quote_entry.locked = True

        nonce_meta = parse_nonce(parsed.nonce)
        if not nonce_meta.value.endswith(f":{sidecar.sidecar_id}"):
            unlock_quote(parsed.quote_id, sidecar)
            return web.json_response({"error": "Nonce sidecar_id mismatch"}, status=402)

        if await sidecar.tx_store.is_processed(parsed.tx_hash):
            unlock_quote(parsed.quote_id, sidecar)
            return web.json_response({"error": "Transaction already used"}, status=409)

        # Block reprocessing of any tx that's already routed to the refund queue.
        # Without this, a /invoke retry could race the refund worker and
        # double-spend the same payment (consume service AND refund).
        pending = await sidecar.refund_queue.get(parsed.tx_hash)
        if pending is not None:
            unlock_quote(parsed.quote_id, sidecar)
            if pending.status == "refunded":
                return web.json_response(
                    {"error": "Transaction already refunded", "refund_tx": pending.refund_tx},
                    status=410,
                )
            if pending.status in ("pending", "refunding"):
                return web.json_response(
                    {"error": "Transaction is queued for refund, do not retry",
                     "refund_pending": True}, status=409,
                )
            if pending.status == "failed":
                return web.json_response(
                    {"error": "Transaction refund failed permanently — contact support",
                     "last_error": pending.last_error}, status=410,
                )
            # 'processed' falls through — should not happen unless racy

        verified = await verify_payment(parsed, sku, sidecar, min_ton, min_usdt)
        if isinstance(verified, web.Response):
            return verified

        try:
            already = await sidecar.tx_store.is_processed(verified.tx_hash)
        except Exception:
            # Money is verified but we can't query tx_store. Don't risk
            # double-processing on retry; let the worker refund.
            logger.exception(
                "tx_store.is_processed failed after verify tx=%s", verified.tx_hash,
            )
            return await enqueue_refund_after_payment(
                sidecar=sidecar, parsed=parsed, sku=sku,
                sender=verified.sender, amount=verified.amount,
                reason="tx_store_unavailable",
            )
        if already:
            unlock_quote(parsed.quote_id, sidecar)
            return web.json_response({"error": "Transaction already used"}, status=409)

        try:
            await sidecar.tx_store.mark_processed(verified.tx_hash)
        except aiosqlite.IntegrityError:
            # A parallel /invoke for the same tx won the PRIMARY KEY race.
            # That request owns the service delivery; we just bow out.
            unlock_quote(parsed.quote_id, sidecar)
            return web.json_response({"error": "Transaction already used"}, status=409)
        except Exception:
            # SQLite write failed (disk full, lock contention beyond 15s, etc.).
            # Money is in but we can't record it. Queue for refund.
            logger.exception(
                "tx_store.mark_processed failed after verify tx=%s", verified.tx_hash,
            )
            return await enqueue_refund_after_payment(
                sidecar=sidecar, parsed=parsed, sku=sku,
                sender=verified.sender, amount=verified.amount,
                reason="mark_processed_failed",
            )

        reservation_key, created_reservation_keys, stock_err = await claim_stock(parsed, sku, sidecar, verified)
        if stock_err is not None:
            return stock_err

        if parsed.quote_id and parsed.quote_id in sidecar.quotes:
            del sidecar.quotes[parsed.quote_id]

        agent_payload = build_agent_payload(parsed, sku)

        runner = create_runner(
            refund_user=sidecar.refund_user,
            refund_queue=sidecar.refund_queue,
            stock=sidecar.stock,
            agent_command=sidecar.settings.agent_command,
            final_timeout=sidecar.settings.final_timeout,
            sidecar_id=sidecar.sidecar_id,
            agent_payload=agent_payload,
            sender=verified.sender,
            amount=verified.amount,
            tx_hash=parsed.tx_hash,
            nonce=parsed.nonce,
            sku_id=sku.sku_id,
            uploaded_files=uploaded_files,
            rail=parsed.rail,
            reservation_key=reservation_key,
            owner_bot=sidecar.owner_bot,
            user_body=parsed.body,
        )
        try:
            job_id = await sidecar.jobs.submit(runner)
        except Exception:
            # Runner never started — mark_processed already ran, so the worker
            # must refund with force=True to bypass the is_processed guard.
            logger.exception(
                "jobs.submit failed after mark_processed tx=%s", verified.tx_hash,
            )
            return await enqueue_refund_after_payment(
                sidecar=sidecar, parsed=parsed, sku=sku,
                sender=verified.sender, amount=verified.amount,
                reason="job_submit_failed",
                force=True,
            )
        # Runner now owns uploaded_files and the reservation; outer finally
        # must not double-clean.
        ownership_transferred = True
        if reservation_key:
            try:
                await sidecar.stock.attach_job(
                    reservation_key, job_id, extend_ttl_seconds=sidecar.settings.final_timeout,
                )
            except Exception:
                logger.exception("attach_job failed")
    finally:
        if not ownership_transferred:
            if uploaded_files:
                cleanup_uploaded_files(uploaded_files)
            for key in created_reservation_keys:
                try:
                    await sidecar.stock.release(key)
                except Exception:
                    logger.exception("Failed to release reservation on early exit")

    return await wait_and_render(job_id, sidecar)
