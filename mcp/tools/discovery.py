"""Discovery tools: list_agents, get_agent_info, ping_agent.

Reuses _fetch_agents_from_chain, _dedupe, _ping_agent, _ping_all from
agents-examples/orchestrator/agents_cache.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from config import REGISTRY_ADDRESS

import aiohttp
from mcp.server.fastmcp import FastMCP

# Import shared logic from orchestrator agents_cache
_ORCH = os.path.join(os.path.dirname(__file__), "..", "..", "agents-examples", "orchestrator")
if _ORCH not in sys.path:
    sys.path.insert(0, _ORCH)

from agents_cache import (  # noqa: E402
    _fetch_agents_from_chain,
    _dedupe,
    _ping_all,
    AgentInfo,
)


def _toncenter_base() -> str:
    testnet = os.getenv("CATALLAXY_TESTNET", "false").lower() == "true"
    return "https://testnet.toncenter.com/api/v3" if testnet else "https://toncenter.com/api/v3"


def _agent_to_dict(a: AgentInfo, pinged: bool = False) -> dict[str, Any]:
    price_ton = f"{a.price / 1e9:.9f}".rstrip("0").rstrip(".")
    d: dict[str, Any] = {
        "sidecar_id": a.sidecar_id,
        "name": a.name,
        "description": a.description,
        "capability": a.capability,
        "capabilities": list(a.capabilities or ((a.capability,) if a.capability else ())),
        "price": a.price,
        "price_ton": price_ton,
        "endpoint": a.endpoint,
        "address": a.address,
        "args_schema": a.args_schema,
        "alive": a.alive,
        "actual_price": a.actual_price,
        "payment_rails": (
            (["TON"] if a.price > 0 else []) + (["USDT"] if a.price_usdt > 0 else [])
        ) or ["TON"],
    }
    if a.price_usdt:
        d["price_usdt"] = a.price_usdt
        d["price_usdt_human"] = f"{a.price_usdt / 1e6:.6f}".rstrip("0").rstrip(".")
    if a.preview_url:
        d["preview_url"] = a.preview_url
    if a.avatar_url:
        d["avatar_url"] = a.avatar_url
    if a.images:
        d["images"] = list(a.images)
    if pinged:
        actual_ton = f"{a.actual_price / 1e9:.9f}".rstrip("0").rstrip(".")
        d["actual_price_ton"] = actual_ton
    return d


def register_discovery_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_agents(
        capability: str | None = None,
        limit: int = 50,
        ping: bool = False,
    ) -> dict:
        """List agents registered in Catallaxy (heartbeat TXs from last 7 days).

        Set ping=true to verify agents are alive and get actual prices (slower).
        """
        api_key = os.getenv("CATALLAXY_TONCENTER_API_KEY")
        base = _toncenter_base()

        agents = await _fetch_agents_from_chain(base, REGISTRY_ADDRESS, api_key)

        if capability:
            agents = [a for a in agents
                      if capability in (a.capabilities or ((a.capability,) if a.capability else ()))]

        agents = agents[:limit]

        pinged = False
        if ping and agents:
            agents = await _ping_all(agents, max_agents=limit)
            pinged = True

        return {
            "agents": [_agent_to_dict(a, pinged) for a in agents],
            "total": len(agents),
            "pinged": pinged,
        }

    @mcp.tool()
    async def get_agent_info(endpoint: str) -> dict:
        """Get detailed info from agent's GET /info endpoint."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{endpoint}/info",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    @mcp.tool()
    async def ping_agent(endpoint: str) -> dict:
        """Check if agent is alive and get actual price + payment address via 402 probe."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/invoke",
                    json={"capability": "", "body": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 402:
                        data = await resp.json()
                        pr = data.get("payment_request") or {}
                        amount = int(pr.get("amount", 0))
                        result: dict[str, Any] = {
                            "alive": True,
                            "actual_price": amount,
                            "actual_price_ton": f"{amount / 1e9:.9f}".rstrip("0").rstrip("."),
                            "payment_address": pr.get("address", ""),
                            "payment_rails": [],
                        }
                        for opt in data.get("payment_options") or []:
                            result["payment_rails"].append(opt.get("rail"))
                            if opt.get("rail") == "USDT":
                                usdt_amt = int(opt.get("amount", 0))
                                result["price_usdt"] = usdt_amt
                                result["price_usdt_human"] = f"{usdt_amt / 1e6:.6f}".rstrip("0").rstrip(".")
                        if not result["payment_rails"]:
                            result["payment_rails"] = ["TON"]
                        return result
                    if resp.status == 400:
                        return {"alive": True, "actual_price": 0, "actual_price_ton": "0", "payment_address": "", "payment_rails": ["TON"]}
                    return {"alive": False, "actual_price": 0, "actual_price_ton": "0", "payment_address": "", "payment_rails": []}
        except Exception:
            return {"alive": False, "actual_price": 0, "actual_price_ton": "0", "payment_address": "", "payment_rails": []}
