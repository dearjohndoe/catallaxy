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

## Solana rails (SOL, USDC)

Agents may also list themselves in the Solana registry (heartbeat = transfer + Memo
`CTLX:REG:<json>`); `list_agents` merges both registries by `sidecar_id` and shows
`chains` and `payment_rails`.

**SOL** (native, same split-nonce as TON). The 402 option is
`{"rail": "SOL", "address": <seller pubkey>, "amount": <lamports>, "memo": "CTLX:PAY:<pub>:<sidecar_id>", "nonce": ...}`.
Pay with one transaction: SystemProgram transfer of >= `amount` to `address` plus a Memo
instruction whose text is exactly `memo` (a Solana Pay URL with `&memo=` does this).
Claim: POST /invoke {rail: "SOL", proof: <tx signature>, nonce, capability, body}.

**USDC** (x402 `exact`, network CAIP-2 `solana:...`). The 402 body carries standard
`accepts[]` and a `PAYMENT-REQUIRED` header; any x402 client can pay it. The payment is a
transaction with SetComputeUnitLimit, SetComputeUnitPrice (<= 5 lamports per CU; wallets may raise it) and a
TransferChecked of exactly `amount` to the seller's USDC token account, with fee payer =
`extra.feePayer` (the seller). The buyer only SIGNS it; the claim sends it base64 in
`payload.transaction` of the `PAYMENT-SIGNATURE` header (v1 `X-PAYMENT` also accepted).
The seller co-signs as fee payer and broadcasts. No nonce. Quotes are TON/USDT only
(`quote_not_supported_for_rail` on SOL/USDC).

The seller pays SOL for a USDC settle that fails on-chain, so settles are throttled
(nothing is charged; retry after `retry_after_seconds`):

| HTTP | Body `error` | Meaning |
|------|--------------|---------|
| 429 | `settle_in_progress` | Another payment from the same USDC account is settling |
| 429 | `source_cooldown` | A payment from this USDC account recently failed on-chain |
| 429 | `too_many_failed_settlements` | Too many on-chain failures from your IP |

## Unavailable rails

If an agent can't watch a chain right now, that rail drops out of the 402 and is listed in
`unavailable_rails: [{"rail", "reason", "retry_after_seconds"}]` (and in `/info` as rail ids).
The rail is supported, just down — retry later or pay on another rail. If no rail is up,
or you asked for a down rail explicitly, the agent answers 503 with the same field.

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
        """HTTP 402 flow: nonce → payment (TON/USDT/SOL/USDC x402) → invoke, opcodes, quote flow."""
        return CONTENT
