from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import api  # late binding for monkeypatched run_agent_subprocess
from api.constants import DYNAMIC_PRICE_CACHE_TTL
from settings import AgentSku

logger = logging.getLogger("sidecar")


@dataclass
class QuoteEntry:
    price: int
    expires_at: float  # unix timestamp
    sku_id: str
    price_usdt: int | None = None
    locked: bool = False


@dataclass
class DynamicPriceCache:
    """Mutable cache shared between handlers and refresh calls."""
    prices: dict[str, dict[str, int]] = field(default_factory=dict)
    refreshed_at: float = 0.0
    lock: asyncio.Lock | None = None


def has_dynamic_skus(skus: list[AgentSku]) -> bool:
    # A SKU is dynamic if any priced rail uses the 0 sentinel. None means the
    # rail is absent (or FREE) and never counts.
    return any(s.price_ton == 0 or s.price_usd == 0 for s in skus)


async def fetch_dynamic_prices(
    cache: DynamicPriceCache,
    *,
    agent_command: str,
    sync_timeout: int,
    sidecar_id: str,
) -> dict[str, dict[str, int]]:
    """Call agent mode=prices and cache for DYNAMIC_PRICE_CACHE_TTL seconds."""
    if cache.lock is None:
        cache.lock = asyncio.Lock()
    now = time.time()
    async with cache.lock:
        if cache.prices and now - cache.refreshed_at < DYNAMIC_PRICE_CACHE_TTL:
            return cache.prices
        try:
            result = await api.run_agent_subprocess(
                command=agent_command,
                payload={"mode": "prices"},
                timeout_seconds=sync_timeout,
                env={"OWN_SIDECAR_ID": sidecar_id},
            )
            prices = result.get("prices")
            if isinstance(prices, dict):
                cache.prices = {k: v for k, v in prices.items() if isinstance(v, dict)}
                cache.refreshed_at = now
                logger.debug("Dynamic prices refreshed: %s", list(cache.prices.keys()))
        except Exception:
            logger.exception("Failed to fetch dynamic prices from agent")
    return cache.prices


def cleanup_expired_quotes(quotes: dict[str, QuoteEntry]) -> None:
    now = time.time()
    expired = [qid for qid, entry in quotes.items() if entry.expires_at <= now]
    for qid in expired:
        del quotes[qid]
