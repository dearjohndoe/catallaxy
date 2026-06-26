from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from settings import SkuKind

from api.domain.quoting import fetch_dynamic_prices, has_dynamic_skus

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


async def handle_info(_: web.Request, sidecar: "SidecarApp") -> web.Response:
    settings = sidecar.settings
    rails = list(settings.payment_rails)

    info: dict[str, Any] = {
        "name": settings.agent_name,
        "description": settings.agent_description,
        "capabilities": list(settings.capabilities),
        "args_schema": sidecar.args_schema,
        "result_schema": sidecar.result_schema,
        "sidecar_id": sidecar.sidecar_id,
        "endpoint": settings.agent_endpoint,
        "payment_rails": rails,
    }
    if settings.has_quote:
        info["has_quote"] = True

    # Always emit skus[] so clients can drive per-SKU UI. Legacy single-SKU
    # agents still see price/price_usdt top-level (populated from that SKU).
    try:
        views = await sidecar.stock.list_views()
    except Exception:
        logger.exception("stock.list_views failed")
        views = []

    # Fetch dynamic prices if any SKU uses ton=0 and usd=0 as the dynamic sentinel.
    dynamic_prices: dict[str, dict[str, int]] = {}
    if has_dynamic_skus(settings.skus):
        try:
            dynamic_prices = await fetch_dynamic_prices(
                sidecar._dynamic_prices_cache,
                agent_command=settings.agent_command,
                sync_timeout=settings.sync_timeout,
                sidecar_id=sidecar.sidecar_id,
            )
        except Exception:
            logger.exception("Dynamic price fetch failed for /info")

    skus_payload: list[dict[str, Any]] = []
    for v in views:
        entry: dict[str, Any] = {
            "id": v.sku_id,
            "title": v.title,
        }
        sku_obj = sidecar._skus_by_id.get(v.sku_id)
        dp = dynamic_prices.get(v.sku_id, {})

        if sku_obj is not None and sku_obj.kind is SkuKind.FREE:
            # FREE SKU: no rails, no price — just flag it. stock_left below still
            # advertises the global cap if one is set.
            entry["free"] = True
        else:
            # Per-rail: a rail priced 0 is dynamic (resolved via mode=prices);
            # a rail priced >0 keeps its fixed amount. This allows mixed SKUs
            # (one fixed rail + one floating rail) — not just all-dynamic ones.
            ton_dynamic = sku_obj is not None and sku_obj.price_ton == 0
            usd_dynamic = sku_obj is not None and sku_obj.price_usd == 0

            price_ton = dp.get("ton") if ton_dynamic else v.price_ton
            price_usd = dp.get("usd") if usd_dynamic else v.price_usd

            if price_ton is not None:
                entry["price_ton"] = price_ton
            if price_usd is not None:
                entry["price_usd"] = price_usd
        if v.stock_left is not None:
            entry["stock_left"] = v.stock_left
        if v.total is not None:
            entry["total"] = v.total
            entry["sold"] = v.sold
        skus_payload.append(entry)
    if skus_payload:
        info["skus"] = skus_payload

    from chains.ton.heartbeat import _valid_image_url
    if settings.agent_preview_url and _valid_image_url(settings.agent_preview_url):
        info["preview_url"] = settings.agent_preview_url
    if settings.agent_avatar_url and _valid_image_url(settings.agent_avatar_url):
        info["avatar_url"] = settings.agent_avatar_url
    if settings.agent_images:
        from chains.ton.heartbeat import MAX_IMAGES
        images = [img for img in settings.agent_images if _valid_image_url(img)]
        if images:
            info["images"] = images[:MAX_IMAGES]
    return web.json_response(info)
