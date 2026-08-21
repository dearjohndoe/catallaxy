from mcp.server.fastmcp import FastMCP

CONTENT = """# HTTP 402 Payment Protocol

## Flow

1. Client POST /invoke {capability, body} (no tx)
2. Sidecar → 402:
   {"error": "Payment required", "payment_options": [
     {"rail": "TON", "address": "UQ...", "amount": "10000000",
      "memo": "pub8hex:sidecar_id", "nonce": "pub8hex+sec8hex:sidecar_id"}
   ]}
   Headers: x-ton-pay-address, x-ton-pay-amount, x-ton-pay-nonce (mirrors `memo`, not `nonce`)

   `memo` and `nonce` are DIFFERENT values, not aliases:
   - `memo` — public-only half. This is what goes on-chain.
   - `nonce` — the full claim value (public half + a secret half that never goes
     on-chain). Only appears here, in the JSON body. Required to claim in step 4.

3. Client sends a TON TX:
   - destination: address, amount: amount
   - body: Cell(uint32=0x50415900, string=memo)   ← use `memo`, NOT `nonce`, here

4. Client POST /invoke {proof, nonce, capability, body}   ← `proof` is v2 (`tx` still accepted as alias).
   Use `nonce` (the full value), NOT `memo`, here.
5. Sidecar verifies: TX exists, amount >= price, claim secret matches, TX not already used
6. Sidecar runs the agent and returns the result

Do not conflate `memo` and `nonce` — sending `nonce` on-chain leaks the secret to any chain
watcher; sending `memo` as the claim value will be rejected (missing secret half).

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

## Free SKUs (giveaways)

Some agents expose a giveaway SKU. In `/info` (and list_agents) it appears as a SKU with
`"free": true`, usually with `stock_left` / `total` / `sold`:

    {"id": "chatgpt-free", "title": "🎁 Free · GPT-5.4 Nano", "free": true, "stock_left": 48, "total": 50}

These need NO on-chain payment and NO TON wallet. Do NOT preflight or invoke_paid them —
call the `invoke_free` tool with the free SKU id. The agent runs immediately and returns the
result (the same shape as a paid call).

Abuse is bounded by a per-IP claim quota plus the global stock cap, so a free claim can fail with:

| HTTP | Body | Meaning |
|------|------|---------|
| 429 | {"error": "free_limit_reached", "retry_after_seconds": N} | Your IP used its free quota; retry later |
| 409 | {"error": "out_of_stock", "sku": ...} | The giveaway is exhausted |

If you accidentally `invoke_free` a paid SKU, the agent answers 402 and the tool tells you to
use preflight + invoke_paid instead.
"""

def register_payment_protocol(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/payment-protocol")
    def payment_protocol() -> str:
        """HTTP 402 flow: nonce → TON TX → invoke, opcodes, quote flow for dynamic pricing."""
        return CONTENT
