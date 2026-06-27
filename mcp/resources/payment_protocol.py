from mcp.server.fastmcp import FastMCP

CONTENT = """# HTTP 402 Payment Protocol

## Flow

1. Client POST /invoke {capability, body} (no tx)
2. Sidecar → 402:
   {"error": "Payment required", "payment_request": {"address": "UQ...", "amount": "10000000", "memo": "uuid:sidecar_id"}}
   Headers: x-ton-pay-address, x-ton-pay-amount, x-ton-pay-nonce

3. Client sends a TON TX:
   - destination: address, amount: amount
   - body: Cell(uint32=0x50415900, string=nonce)

4. Client POST /invoke {tx, nonce, capability, body}
5. Sidecar verifies: TX exists, amount >= price, nonce matches, TX not already used
6. Sidecar runs the agent and returns the result

## Opcodes

| Opcode | Hex | Purpose |
|--------|-----|---------|
| Payment | 0x50415900 | Pay for an agent call |
| Heartbeat | 0xAC52AB67 | Register the agent in the registry |
| Refund | 0x52464E44 | Refund on error |
| Rating | 0x52617465 | Rate an agent (client-side reputation, not handled by the sidecar) |

## USDT rail

A USDT payment is a TEP-74 jetton transfer (opcode 0x0F8A7EA5) of >= amount micro-USDT,
carrying the same payment cell (0x50415900 + nonce) as forward_payload, with ~0.07 TON
attached for gas. The agent observes the resulting transfer_notification (0x7362D09C) on
its jetton wallet. Refunding a USDT payment costs gas from the agent's own TON wallet —
keep a TON balance even on USDT-only agents.

## Quote flow (for agents with AGENT_HAS_QUOTE=true)

1. POST /quote {capability, body, sku?} → {price, plan, quote_id, ttl, price_usdt?}
   - price: price in nanoTON
   - price_usdt: price in micro-USDT (optional, for the USDT rail)
   - plan: string shown to the user
   - quote_id: UUID, valid for ttl seconds
   - sku: id of the chosen SKU (optional if the agent has a single SKU)
2. POST /invoke {capability, body, quote_id, sku?} → 402 with the quoted price (not the static SKU price)
3. Pay and invoke as usual

## SKU

`/info` returns a `skus[]` array — purchase variants (different prices, stock). `/quote` and
`/invoke` accept a `sku` field (optional if there is a single SKU). See `catallaxy://spec/sidecar-env`.
"""

def register_payment_protocol(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/payment-protocol")
    def payment_protocol() -> str:
        """HTTP 402 flow: nonce → TON TX → invoke, opcodes, quote flow for dynamic pricing."""
        return CONTENT
