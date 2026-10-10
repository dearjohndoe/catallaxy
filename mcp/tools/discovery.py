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
from lib.solana import fetch_registry_agents, lamports_to_sol, micro_to_usdc

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


def _rails(a: AgentInfo) -> list[str]:
    if a.rails:
        return list(a.rails)
    rails = (
        (["TON"] if a.price > 0 else []) + (["USDT"] if a.price_usdt > 0 else [])
        + (["SOL"] if a.price_sol > 0 else []) + (["USDC"] if a.price_usdc > 0 else [])
    )
    return rails or ["TON"]


def _agent_to_dict(a: AgentInfo, pinged: bool = False) -> dict[str, Any]:
    price_ton = f"{a.price / 1e9:.9f}".rstrip("0").rstrip(".")
    rails = _rails(a)
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
        "payment_rails": rails,
        "chains": list(a.chains),
    }
    if a.price_usdt:
        d["price_usdt"] = a.price_usdt
        d["price_usdt_human"] = f"{a.price_usdt / 1e6:.6f}".rstrip("0").rstrip(".")
    if a.price_sol:
        d["price_sol"] = a.price_sol
        d["price_sol_human"] = lamports_to_sol(a.price_sol)
    if a.price_usdc:
        d["price_usdc"] = a.price_usdc
        d["price_usdc_human"] = micro_to_usdc(a.price_usdc)
    if a.preview_url:
        d["preview_url"] = a.preview_url
    if a.avatar_url:
        d["avatar_url"] = a.avatar_url
    if a.images:
        d["images"] = list(a.images)
    if pinged and "TON" in rails:
        actual_ton = f"{a.actual_price / 1e9:.9f}".rstrip("0").rstrip(".")
        d["actual_price_ton"] = actual_ton
    return d


def _solana_listing_to_agent(listing: dict[str, Any]) -> AgentInfo | None:
    p = listing.get("payload") or {}
    if not isinstance(p.get("endpoint"), str) or not isinstance(p.get("sidecar_id"), str):
        return None
    caps = tuple(str(c) for c in p.get("capabilities") or () if c)
    return AgentInfo(
        sidecar_id=p["sidecar_id"],
        name=str(p.get("name") or ""),
        description="",
        capability=caps[0] if caps else "",
        price=int(p.get("price") or p.get("price_hint") or 0),
        actual_price=0,
        endpoint=p["endpoint"],
        address="",  # the Solana listing does not name the signer
        price_usdt=int(p.get("price_usdt") or 0),
        capabilities=caps,
        rails=tuple(str(r).upper() for r in p.get("rails") or () if r),
        price_sol=int(p.get("price_sol") or 0),
        price_usdc=int(p.get("price_usdc") or 0),
        chains=("solana",),
    )


def merge_registries(ton: list[AgentInfo], solana: list[AgentInfo]) -> list[AgentInfo]:
    """One entry per ``sidecar_id``: TON listing first (it carries the TON
    address used for ratings), Solana fills prices/rails it lacks."""
    by_sid = {a.sidecar_id: a for a in ton}
    out = list(ton)
    for s in solana:
        t = by_sid.get(s.sidecar_id)
        if t is None:
            out.append(s)
            continue
        t.chains = tuple(dict.fromkeys((*t.chains, "solana")))
        t.rails = tuple(dict.fromkeys((*t.rails, *s.rails)))
        t.price_sol = t.price_sol or s.price_sol
        t.price_usdc = t.price_usdc or s.price_usdc
    return out


def register_discovery_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_agents(
        capability: str | None = None,
        limit: int = 50,
        ping: bool = False,
    ) -> dict:
        """List agents registered in Catallaxy (heartbeats from the last 7 days in
        the TON registry and, when configured, the Solana registry; merged by
        sidecar_id). `chains` says where each agent is listed; `payment_rails`
        says how it can be paid: TON, USDT, SOL, USDC.

        Set ping=true to verify agents are alive and get actual prices (slower).
        """
        api_key = os.getenv("CATALLAXY_TONCENTER_API_KEY")
        base = _toncenter_base()

        ton_res, sol_res = await asyncio.gather(
            _fetch_agents_from_chain(base, REGISTRY_ADDRESS, api_key),
            fetch_registry_agents(),
            return_exceptions=True,
        )
        if isinstance(ton_res, BaseException) and isinstance(sol_res, BaseException):
            raise ton_res
        ton_agents = [] if isinstance(ton_res, BaseException) else ton_res
        sol_agents = [] if isinstance(sol_res, BaseException) else [
            a for a in map(_solana_listing_to_agent, sol_res) if a is not None
        ]
        agents = merge_registries(ton_agents, sol_agents)

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
                            elif opt.get("rail") == "SOL":
                                result["price_sol"] = int(opt.get("amount", 0))
                                result["price_sol_human"] = lamports_to_sol(result["price_sol"])
                            elif opt.get("rail") == "USDC":
                                result["price_usdc"] = int(opt.get("amount", 0))
                                result["price_usdc_human"] = micro_to_usdc(result["price_usdc"])
                        if not result["payment_rails"]:
                            result["payment_rails"] = ["TON"]
                        return result
                    if resp.status == 400:
                        return {"alive": True, "actual_price": 0, "actual_price_ton": "0", "payment_address": "", "payment_rails": ["TON"]}
                    return {"alive": False, "actual_price": 0, "actual_price_ton": "0", "payment_address": "", "payment_rails": []}
        except Exception:
            return {"alive": False, "actual_price": 0, "actual_price_ton": "0", "payment_address": "", "payment_rails": []}
