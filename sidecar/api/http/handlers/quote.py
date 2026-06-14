from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from settings import AgentSku

import api  # late binding for monkeypatched run_agent_subprocess
from api.constants import DEFAULT_QUOTE_TTL
from api.domain.pricing import resolve_sku
from api.domain.quoting import QuoteEntry, cleanup_expired_quotes
from api.http.multipart import parse_multipart_invoke
from api.validation import validate_body

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


@dataclass
class ParsedQuote:
    capability: str
    sku_field: str | None
    body: dict[str, Any]


async def _parse_quote_request(
    request: web.Request, sidecar: "SidecarApp"
) -> ParsedQuote | web.Response:
    try:
        if request.content_type and "multipart/form-data" in request.content_type:
            _, _, capability, _, _, sku_field, body, _ = await parse_multipart_invoke(
                request, sidecar._file_store_dir,
            )
        else:
            data = await request.json()
            capability = str(data.get("capability", "")).strip()
            sku_field = str(data.get("sku", "")).strip() or None
            body = data.get("body", {})
    except Exception:
        return web.json_response({"error": "Invalid request"}, status=400)
    return ParsedQuote(capability=capability, sku_field=sku_field, body=body)


async def _run_quote_agent(
    sku: AgentSku, parsed: ParsedQuote, sidecar: "SidecarApp"
) -> dict[str, Any] | web.Response:
    quote_payload = {
        "mode": "quote",
        "capability": parsed.capability,
        "sku": sku.sku_id,
        "body": parsed.body,
    }
    try:
        return await api.run_agent_subprocess(
            command=sidecar.settings.agent_command,
            payload=quote_payload,
            timeout_seconds=sidecar.settings.sync_timeout,
            env={"OWN_SIDECAR_ID": sidecar.sidecar_id},
        )
    except Exception:
        logger.exception("Quote subprocess failed")
        return web.json_response({"error": "Quote generation failed"}, status=500)


def _parse_quote_result(
    agent_result: dict[str, Any]
) -> tuple[int, str, str | None, int, int | None] | web.Response:
    price = agent_result.get("price")
    if not isinstance(price, int) or price <= 0:
        return web.json_response({"error": "Agent returned invalid price"}, status=500)
    plan = agent_result.get("plan", "")
    note = agent_result.get("note")
    ttl = int(agent_result.get("ttl", DEFAULT_QUOTE_TTL))
    price_usdt = agent_result.get("price_usdt")
    if price_usdt is not None and (not isinstance(price_usdt, int) or price_usdt <= 0):
        price_usdt = None
    return price, plan, note, ttl, price_usdt


async def _reserve_quote_stock(
    sku: AgentSku, quote_id: str, ttl: int, sidecar: "SidecarApp"
) -> web.Response | None:
    """Reserve stock for the quote. Returns None on success, error response on failure."""
    reserve_ttl = max(ttl, sidecar.settings.payment_timeout)
    try:
        ok = await sidecar.stock.reserve(sku.sku_id, quote_id, reserve_ttl)
    except Exception:
        logger.exception("stock.reserve failed during quote")
        return web.json_response({"error": "Internal stock error"}, status=500)
    if not ok:
        return web.json_response({"error": "out_of_stock", "sku": sku.sku_id}, status=409)
    return None


async def handle_quote(request: web.Request, sidecar: "SidecarApp") -> web.Response:
    if not sidecar.settings.has_quote:
        return web.json_response({"error": "This agent does not support quotes"}, status=404)

    parsed = await _parse_quote_request(request, sidecar)
    if isinstance(parsed, web.Response):
        return parsed

    if not parsed.capability:
        return web.json_response({"error": "capability is required"}, status=400)
    if parsed.capability not in sidecar.settings.capabilities:
        return web.json_response({"error": "Unsupported capability"}, status=400)

    sku, sku_err = resolve_sku(
        parsed.sku_field, sidecar._skus_by_id, sidecar._single_sku, sidecar.settings.skus,
    )
    if sku_err is not None:
        return sku_err
    assert sku is not None

    missing = validate_body({"body": parsed.body}, sidecar.args_schema)
    if missing:
        return web.json_response({"error": "Missing required fields", "missing": missing}, status=400)

    # Pre-check stock before calling agent — cheap rejection path.
    view = await sidecar.stock.get_view(sku.sku_id)
    if view.stock_left is not None and view.stock_left <= 0:
        return web.json_response({"error": "out_of_stock", "sku": sku.sku_id}, status=409)

    agent_result = await _run_quote_agent(sku, parsed, sidecar)
    if isinstance(agent_result, web.Response):
        return agent_result

    parsed_result = _parse_quote_result(agent_result)
    if isinstance(parsed_result, web.Response):
        return parsed_result
    price, plan, note, ttl, price_usdt = parsed_result

    cleanup_expired_quotes(sidecar.quotes)

    quote_id = str(uuid.uuid4())
    expires_at = time.time() + ttl

    stock_err = await _reserve_quote_stock(sku, quote_id, ttl, sidecar)
    if stock_err is not None:
        return stock_err

    sidecar.quotes[quote_id] = QuoteEntry(
        price=price, expires_at=expires_at, sku_id=sku.sku_id, price_usdt=price_usdt,
    )

    resp: dict[str, Any] = {
        "quote_id": quote_id,
        "price": price,
        "plan": plan,
        "sku": sku.sku_id,
        "expires_at": int(expires_at),
    }
    if price_usdt:
        resp["price_usdt"] = price_usdt
    if note:
        resp["note"] = note

    return web.json_response(resp)
