import asyncio
from urllib.parse import quote
import aiohttp
from mcp.server.fastmcp import FastMCP
from lib.cell_builder import build_payment_cell, build_jetton_transfer_cell

def _ascii_qr(data: str) -> str:
    """Render a payment URL as a scannable ASCII/Unicode QR code.

    Uses half-block characters so the code stays compact enough to scan
    straight off a terminal. Returns "" if qrcode is unavailable so the
    rest of preflight degrades gracefully.
    """
    try:
        import io
        import qrcode

        qr = qrcode.QRCode(
            border=2,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
        )
        qr.add_data(data)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue().rstrip("\n")
    except Exception:
        return ""


def register_invocation_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    async def preflight(
        endpoint: str,
        capability: str,
        body: dict,
        quote_id: str | None = None,
        rail: str = "TON",
        user_address: str | None = None,
        sku: str | None = None,
    ) -> dict:
        """Initiate agent call: get payment details and build Cell payload for @ton/mcp.

        rail: "TON" (default) or "USDT".
        sku: optional SKU id — required if the agent exposes multiple SKUs without a quote_id.
        user_address: required when rail="USDT" — your wallet address (for USDT refunds).
        Returns payment_options (all available rails) plus ready-to-use payload for chosen rail.

        For the TON rail the result also includes ready-to-sign deeplinks (pay_url,
        pay_url_tonkeeper) and a scannable ASCII QR (pay_qr) — these carry the payload
        as `bin=`, the only correct way to pay by hand. The `how_to_pay` field tells you
        whether to pay from your own wallet (if a TON MCP is connected) or hand off to
        the user. Never send the nonce as a plain text comment — it won't be matched.
        """
        payload: dict = {"capability": capability, "body": body, "rail": rail}
        if quote_id:
            payload["quote_id"] = quote_id
        if sku:
            payload["sku"] = sku
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{endpoint}/invoke",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    # FREE SKU: the agent ran with no payment instead of returning 402.
                    # preflight is the wrong tool for these — surface the result we
                    # already got and point the caller at invoke_free.
                    data = await resp.json()
                    return {
                        "free": True,
                        "note": (
                            f"'{sku or 'default'}' is a FREE SKU — no payment needed. "
                            "Use invoke_free for these; preflight/invoke_paid are for paid "
                            "SKUs. The result below was produced by this call."
                        ),
                        "status": data.get("status"),
                        "result": data.get("result"),
                        "job_id": data.get("job_id"),
                    }
                if resp.status != 402:
                    text = await resp.text()
                    raise ValueError(f"Expected 402, got {resp.status}: {text}")
                data = await resp.json()

        payment_options = data.get("payment_options") or []
        # Fall back to legacy payment_request if no payment_options
        if not payment_options:
            pr = data.get("payment_request") or {}
            payment_options = [{"rail": "TON", **pr}]

        # Find the chosen rail
        opt = next((o for o in payment_options if o.get("rail") == rail), None)
        if opt is None:
            available = [o.get("rail") for o in payment_options]
            raise ValueError(f"Rail '{rail}' not available. Agent supports: {available}")

        nonce = opt.get("memo", "")
        result: dict = {
            "rail": rail,
            "nonce": nonce,
            "payment_options": payment_options,
        }

        if rail == "USDT":
            agent_address = opt.get("address", "")
            usdt_amount = int(opt.get("amount", 0))
            if not user_address:
                raise ValueError("user_address required for USDT rail (used as refund destination)")
            payload_b64, payload_hex = build_jetton_transfer_cell(
                agent_address=agent_address,
                usdt_amount=usdt_amount,
                nonce=nonce,
                response_destination=user_address,
            )
            result.update({
                "agent_address": agent_address,
                "usdt_amount": usdt_amount,
                "usdt_amount_human": f"{usdt_amount / 1e6:.6f}".rstrip("0").rstrip("."),
                # Send payload + ~0.07 TON gas to your own USDT jetton wallet
                "attached_ton": "70000000",
                "attached_ton_human": "0.07",
                "payload_base64": payload_b64,
                "payload_hex": payload_hex,
                "note": "Send payload to YOUR OWN USDT jetton wallet (not agent address) with attached_ton as gas.",
                "how_to_pay": (
                    "If you have a TON wallet available (e.g. a connected TON MCP), "
                    "offer the user a choice: pay it yourself (send the jetton transfer "
                    "with payload_base64 + attached_ton gas to your own USDT jetton "
                    "wallet, then call invoke_paid with rail='USDT' and the tx_hash), "
                    "or let the user pay. If you have no wallet, hand the payload + "
                    "instructions to the user."
                ),
            })
        else:
            address = opt.get("address", "")
            amount = str(opt.get("amount", "0"))
            payload_b64, payload_hex = build_payment_cell(nonce)
            # Ready-to-sign deeplinks. MUST carry the payload as `bin=` (raw BoC),
            # NOT `text=` — a plain text comment uses opcode 0x00000000, but the
            # agent's payment monitor only indexes transactions whose comment cell
            # starts with PAYMENT_OPCODE (0x50415900). A `text=` deeplink silently
            # fails verification with "Transaction not found".
            bin_q = quote(payload_b64, safe="")
            pay_url = f"ton://transfer/{address}?amount={amount}&bin={bin_q}"
            pay_url_tonkeeper = (
                f"https://app.tonkeeper.com/transfer/{address}?amount={amount}&bin={bin_q}"
            )
            result.update({
                "address": address,
                "amount": amount,
                "amount_ton": f"{int(amount) / 1e9:.9f}".rstrip("0").rstrip("."),
                "payload_base64": payload_b64,
                "payload_hex": payload_hex,
                "pay_url": pay_url,
                "pay_url_tonkeeper": pay_url_tonkeeper,
                "pay_qr": _ascii_qr(pay_url),
                "warning": (
                    "To pay by hand, use pay_url/pay_url_tonkeeper or send the "
                    "payload_base64 as the message body (bin). Do NOT send the nonce "
                    "as a plain text comment — it will not be matched."
                ),
                "how_to_pay": (
                    "If you have a TON wallet available (e.g. a connected TON MCP), "
                    "check your balance and offer the user a choice: pay it yourself "
                    f"({result['rail']}: send {amount} nanoton with payload_base64 as "
                    "the message body to `address`, then call invoke_paid with the "
                    "resulting tx_hash), or let the user pay by showing them pay_qr / "
                    "pay_url. If you have no wallet, show pay_qr / pay_url to the user."
                ),
            })

        return result

    @mcp.tool()
    async def invoke_paid(
        endpoint: str,
        tx_hash: str,
        nonce: str,
        capability: str,
        body: dict,
        quote_id: str | None = None,
        rail: str = "TON",
        auto_poll: bool = True,
        poll_timeout: int = 300,
        sku: str | None = None,
    ) -> dict:
        """Call agent with proof of payment (TX hash from @ton/mcp).

        rail: "TON" (default) or "USDT" — must match the rail used in preflight.
        sku: optional SKU id — must match the one used in preflight/quote.
        """
        payload: dict = {"tx": tx_hash, "nonce": nonce, "capability": capability, "body": body, "rail": rail}
        if quote_id:
            payload["quote_id"] = quote_id
        if sku:
            payload["sku"] = sku
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{endpoint}/invoke",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                http_status = resp.status
                data = await resp.json()

        status = data.get("status")
        if status == "done":
            return {"status": "done", "result": data.get("result"), "job_id": data.get("job_id")}
        if status == "refunded":
            return data

        job_id = data.get("job_id") or data.get("id")
        if http_status >= 400 or "error" in data:
            raise ValueError(f"invoke failed (HTTP {http_status}): {data}")
        if not job_id:
            raise ValueError(f"invoke returned no job_id (HTTP {http_status}): {data}")
        if not auto_poll:
            return {"status": status or "pending", "job_id": job_id, "poll_endpoint": f"GET /result/{job_id}"}

        # auto poll
        deadline = asyncio.get_event_loop().time() + poll_timeout
        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1)
                async with session.get(
                    f"{endpoint}/result/{job_id}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()
                status = result.get("status")
                if status in ("done", "error", "refunded"):
                    return result
        return {"status": "pending", "job_id": job_id, "error": "poll_timeout"}

    @mcp.tool()
    async def invoke_free(
        endpoint: str,
        capability: str,
        body: dict,
        sku: str | None = None,
        auto_poll: bool = True,
        poll_timeout: int = 300,
    ) -> dict:
        """Claim a FREE SKU — no payment, no TON wallet, no preflight needed.

        Some agents expose giveaway SKUs. In `/info` (or list_agents) these show up as
        a SKU with `"free": true`, usually alongside `stock_left` / `total` / `sold`.
        They cost nothing: do NOT call preflight or invoke_paid for them — just call this
        with the free SKU id.

        The agent gates free claims by a per-IP quota plus a global stock cap, so besides
        a normal result this may return:
          - {"error": "free_limit_reached", "retry_after_seconds": N}  — your IP hit its quota
          - {"error": "out_of_stock", "sku": ...}                      — the giveaway is exhausted
        On success it returns the agent result, same shape as invoke_paid.
        """
        payload: dict = {"capability": capability, "body": body}
        if sku:
            payload["sku"] = sku
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{endpoint}/invoke",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                http_status = resp.status
                data = await resp.json()

        # Paid SKU mistakenly called as free → agent answers 402. Point the caller right.
        if http_status == 402 or "payment_request" in data or data.get("payment_options"):
            raise ValueError(
                f"'{sku}' is not a free SKU — it requires payment. "
                "Use preflight (to get pay_url/pay_qr) + invoke_paid instead."
            )

        status = data.get("status")
        if status == "done":
            return {"status": "done", "result": data.get("result"), "job_id": data.get("job_id")}
        # Gate rejections (free_limit_reached / out_of_stock) — surface verbatim.
        if "error" in data:
            return data
        if http_status >= 400:
            raise ValueError(f"free invoke failed (HTTP {http_status}): {data}")

        job_id = data.get("job_id") or data.get("id")
        if not job_id:
            raise ValueError(f"free invoke returned no job_id (HTTP {http_status}): {data}")
        if not auto_poll:
            return {"status": status or "pending", "job_id": job_id, "poll_endpoint": f"GET /result/{job_id}"}

        deadline = asyncio.get_event_loop().time() + poll_timeout
        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1)
                async with session.get(
                    f"{endpoint}/result/{job_id}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()
                status = result.get("status")
                if status in ("done", "error", "refunded"):
                    return result
        return {"status": "pending", "job_id": job_id, "error": "poll_timeout"}

    @mcp.tool()
    async def poll_result(endpoint: str, job_id: str) -> dict:
        """Poll async job result."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{endpoint}/result/{job_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()

    @mcp.tool()
    async def get_quote(endpoint: str, capability: str, body: dict, sku: str | None = None) -> dict:
        """Get price quote from agent (for agents with dynamic pricing).

        sku: optional SKU id — required if the agent exposes more than one SKU.
        """
        payload: dict = {"capability": capability, "body": body}
        if sku:
            payload["sku"] = sku
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{endpoint}/quote",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
        price = data.get("price", 0)
        if price:
            data["price_ton"] = f"{price / 1e9:.9f}".rstrip("0").rstrip(".")
        price_usdt = data.get("price_usdt", 0)
        if price_usdt:
            data["price_usdt_human"] = f"{price_usdt / 1e6:.6f}".rstrip("0").rstrip(".")
        return data
