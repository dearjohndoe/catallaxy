# Catallaxy Protocol v1 (CTLX/1)

> [Русская версия](PROTOCOL.ru.md)

Status: **stable, deployed** (TON mainnet). This document describes the protocol as implemented; it supersedes the draft spec in `PROJECT.md` (v0.2, historical).

Catallaxy is a permissionless protocol for selling machine-callable services and digital goods for on-chain payments. It consists of three independent layers:

1. **Registry** — on-chain agent registration via heartbeat transactions.
2. **Invocation** — an HTTP 402 payment-and-call flow between a client and an agent.
3. **Reputation** — optional on-chain payment/refund/rating signals.

Any layer can be implemented independently. A conforming *agent* is any HTTP server implementing §4–§6; a conforming *client* is anything that can read the registry and pay on-chain. No smart contracts are required.

## 1. Terms

| Term | Meaning |
|---|---|
| **Agent** | A service sold on the marketplace, identified by the pair (`sidecar_id`, wallet address). The wallet receives payments and sends heartbeats/refunds; it need not be unique per agent — attribution between agents sharing a wallet is carried by the `sidecar_id` suffix in nonces and payloads. |
| **Sidecar** | Reference implementation of the agent side (wraps any stdin/stdout script). |
| **Registry** | A well-known wallet address. The registry never signs anything; it is only a mailbox whose incoming transactions are read by clients. |
| **Rail** | A payment route: `TON` (native) or `USDT` (TEP-74 jetton) in v1. |
| **Client** | A buyer: a human frontend, an MCP-driven LLM, or another agent. |
| **SKU** | A priced unit of inventory within an agent (an agent has ≥1 SKU). |

All protocol payloads embedded in transactions are TON cells: a 32-bit opcode followed by a snake-encoded string.

## 2. Opcodes

| Action | Opcode | Body after opcode |
|---|---|---|
| Heartbeat (register/update) | `0xAC52AB67` | JSON, ≤ 2048 bytes |
| Payment | `0x50415900` (`PAY\0`) | nonce string |
| Refund | `0x52464E44` (`RFND`) | JSON `{"sidecar_id": ...}` |
| Rating | `0x52617465` (`Rate`) | string containing `sidecar:{sidecar_id}` and `score:{1-5}` |

## 3. Registry layer

**Registration = heartbeat.** The agent sends a transaction (any small amount, reference: 0.01 TON) to the registry address with opcode `0xAC52AB67` and a JSON payload:

```json
{
  "sidecar_id": "…",            // required, stable identity
  "endpoint": "https://…",      // required, base URL of the agent HTTP API
  "name": "…",
  "rails": ["TON", "USDT"],     // advertised payment rails
  "price_hint": 10000000,       // advisory; same units as the first priced rail
  "price": 10000000,            // CTLX/1 alias of price_hint (dual-written)
  "capabilities": ["…"],        // short tags for listing; optional
  "price_usdt": 1000000,        // optional USDT hint (micro-USDT)
  "has_quote": true,            // optional
  "owner_wallet": "…"           // optional; clients SHOULD cross-check 402 address
}
```

On-chain payload MUST stay ≤ ~900 bytes (Solana memo budget; TON can do more but we share the format). `args_schema`, `result_schema`, `description`, and media (`preview_url` / `avatar_url` / `images`) MUST NOT be written on-chain — clients fetch them from `GET /info`.

Readers MUST still accept CTLX/1 fat heartbeats (those fields present) for N months of mixed registry history.

**Liveness window: 7 days.** Clients consider an agent listed iff a heartbeat exists within the last 7 days. Update = new heartbeat (latest wins). Deregistration = silence.

**Discovery rule (client side):** fetch incoming transactions to the registry for the last 7 days, filter by opcode `0xAC52AB67`, parse JSON, drop entries without `endpoint` or `sidecar_id`, deduplicate by `sidecar_id` keeping the newest. Treat `price_hint` as `price` when `price` is absent.

The registry address is a client-side configuration default, not a protocol constant: anyone can run an alternative registry by pointing clients at a different address.

## 4. Agent HTTP API

| Route | Purpose |
|---|---|
| `POST /invoke` | Preflight (no payment) → 402; paid call (with proof) → result |
| `POST /quote` | Optional: lock a price/stock quote, returns `quote_id` |
| `GET /result/{job_id}` | Poll an async job |
| `GET /download/{file_id}` | Fetch a file produced by a job |
| `GET /info` | Live metadata: name, capabilities, `args_schema`, `payment_rails`, `skus[]` with current prices/stock |

`/info` is authoritative for prices, stock, schemas, description, and media; heartbeat values are advisory hints for listings.

## 5. Invocation flow (HTTP 402)

```
Client                                Agent
  │ POST /invoke {capability, body,     │
  │   rail, sku?}                       │
  │────────────────────────────────────►│
  │ 402 {payment_request,               │
  │      payment_options[]}             │
  │◄────────────────────────────────────│
  │                                     │
  │   on-chain payment tx (§6)          │
  │                                     │
  │ POST /invoke {proof, nonce, rail,   │
  │   capability, body, sku?}           │
  │────────────────────────────────────►│
  │ 200 {status:"done", result}         │
  │   or {job_id, status:"pending"}     │
  │◄────────────────────────────────────│
  │ GET /result/{job_id}  (if pending)  │
```

**402 response body:**

```json
{
  "error": "Payment required",
  "payment_request": { … },        // first of payment_options, legacy
  "payment_options": [
    { "rail": "TON",  "address": "EQ…", "amount": "10000000",
      "memo": "<pub:sidecar_id>", "nonce": "<pub+sec:sidecar_id>", "sku": "default" },
    { "rail": "USDT", "address": "EQ…", "amount": "1000000",
      "memo": "<pub:sidecar_id>", "nonce": "<pub+sec:sidecar_id>", "sku": "default",
      "token": {"symbol": "USDT", "master": "EQ…", "decimals": 6} }
  ]
}
```

Headers `x-ton-pay-address`, `x-ton-pay-amount`, `x-ton-pay-nonce` duplicate the TON option for header-only clients.

**Nonce:** `{16 hex chars}:{sidecar_id}`. The suffix binds a payment to one agent: clients MUST NOT pay against a nonce whose suffix differs from the agent's `sidecar_id`; agents MUST reject foreign nonces.

The 16 hex chars are `pub(8) + sec(8)` (v2 normative, see §6.1). `pub` is the on-chain correlator
— it's what actually goes in the payment memo/cell. `sec` is a capability secret that travels only
in the 402 response body (a new `payment_options[].nonce` field, distinct from `memo`) and must
never be written on-chain. Clients MUST use `memo` (public-only) to build the payment tx, and MUST
hold on to `payment_options[].nonce` (the full `pub+sec` value) to present at claim time. Treating
`memo` and the claim nonce as interchangeable — as CTLX/1 clients did — breaks claiming under
CTLX/2.

`x-ton-pay-nonce` (header-only client affordance) carries the same value as `memo` — the
public-only half — **not** the full claim nonce. A header-only client that only reads headers has
no way to claim under CTLX/2 today (it would need the `sec` half, which only exists in the JSON
body); this is a known gap, not currently exercised by our own clients (frontend/mcp both parse the
JSON body). Revisit with a second header if a real header-only client shows up.

**Error semantics:**

| Status | Meaning |
|---|---|
| `402` | No/invalid payment proof; body carries payment options |
| `409 {"error":"out_of_stock"}` | SKU not purchasable (also returned when dynamic pricing yields no price) |
| `409` (tx already processed) | Replay of a consumed `tx` |
| `503` + `Retry-After` | Agent cannot currently verify payments (chain monitor degraded). Agents MUST refuse to issue 402s in this state rather than accept unverifiable payments |

## 6. Payment transactions

**TON rail.** A transfer to `address` of ≥ `amount` nanoTON whose message body is a cell: `0x50415900` (32 bits) ++ nonce as snake string. The nonce travels in the body cell, **not** a text comment.

**USDT rail.** A standard TEP-74 jetton transfer (opcode `0x0F8A7EA5`) of ≥ `amount` micro-USDT sent to the *payer's own* jetton wallet, with the same payment cell as `forward_payload` and ~0.07 TON attached for gas. The agent observes the resulting `transfer_notification` (`0x7362D09C`) on its jetton wallet.

**Payment proof (v2).** Claim body is `{rail, proof, nonce}`. `proof` is rail-specific: a TON tx hash, a Solana signature, or the contents of an x402 `X-PAYMENT` header. `tx` is a CTLX/1 alias of `proof` and MUST be accepted when `proof` is absent; if both are present, `proof` wins.

**Verification rules (agent side).** A payment proof `{proof, nonce}` is valid iff all hold:
1. the transaction exists and is finalized on-chain;
2. recipient is the agent's wallet (TON) or the agent's USDT jetton wallet (USDT);
3. transferred amount ≥ quoted `amount` for the chosen rail and SKU;
4. the body/forward_payload carries opcode `0x50415900` and a nonce equal to the one issued, with the agent's own `sidecar_id` suffix;
5. the payment is younger than the agent's payment timeout;
6. `tx` has never been consumed before (single-use; agents MUST persist consumed tx hashes durably).

### 6.1 Claim-secret check (v2 normative)

Before rule 1 runs, the agent MUST split the presented nonce into `pub`/`sec` (§5) and check `sec`
against the secret it minted for `pub` at 402 time (e.g. a hash, held in a short-lived server-side
map keyed by `pub`, TTL = payment timeout). On mismatch or expiry: reject with `401`/`403`, and the
rejection MUST NOT mark any tx processed, consume the nonce, or enqueue a refund — a subsequent
claim with the correct secret must still succeed. Only on a matching secret does the agent proceed
to look the tx up on-chain, keyed by `pub` (that's what's actually in the memo/cell).

This closes front-running by chain watchers: `{tx, pub}` alone (everything visible on-chain) is
insufficient to claim, since `sec` never appears on-chain — only in the 402 response delivered to
whoever requested it. It's a bearer-secret model, not a wallet-signature model: it proves the
claimant received the 402 response, not that they hold the paying wallet's key. (A per-invocation
wallet-signature scheme was considered and rejected — both TonConnect and Solana wallet-standard
adapters require a fresh popup confirmation per signed message, on top of the payment-tx
confirmation the buyer already does; that UX cost was judged unacceptable for this threat's
severity.)

## 7. Refunds

If a verified payment cannot be serviced (agent failure, lost race on stock, unsupported rail), the agent SHOULD refund the payer from its own wallet: payment amount minus a fixed fee, with opcode `0x52464E44` and JSON body `{"sidecar_id": …}` so the refund is attributable on-chain. Refunds are best-effort and retried with backoff; v1 has **no escrow** — see §9.

## 8. Reputation layer (optional)

Any wallet MAY send a rating transaction to the agent's wallet: opcode `0x52617465`, body containing `sidecar:{sidecar_id}` and `score:{1-5}`. Clients compute reputation from on-chain history; the reference weighting is: payment `+2.0`, refund `−2.1`, rating `(score−3)×1.25`, normalized to 0–10. Weighting is client policy, not protocol.

## 9. Security considerations

- **Prepayment trust model.** The client pays before delivery; refunds are voluntary. v1 is economically suited to low-value calls where reputation, not escrow, prices the risk. Escrow is a v2 candidate.
- **Replay.** Single-use tx hashes (§6.6) and agent-bound nonces (§5) prevent cross-call and cross-agent replay.
- **Claim front-running.** v1 nonces are fully public on-chain (memo = full nonce), so a chain
  watcher can race the real payer's own claim and win. v2 splits the nonce (§5, §6.1): only the
  correlator half goes on-chain, the capability-secret half travels solely in the 402 response.
- **Sybil ratings.** Reputation inputs are permissionless; clients SHOULD discount self-dealing patterns (payments from agent-affiliated wallets).
- **Proxied 402 substitution.** If the agent is reached through a reverse proxy, the proxy can rewrite `payment_options`. Clients SHOULD cross-check `address` against `owner_wallet` from the heartbeat when present; response signing is planned for v2.
- **Registry spam.** Listing is permissionless; clients MUST treat heartbeat content as untrusted input (schema size caps, URL sanitization).

## 10. Versioning

This is CTLX/1 on the wire for TON cells/opcodes/jettons. CTLX/2 write-path pieces that have landed on the TON side: thin heartbeat (§3), split-nonce (§5 / §6.1), generic proof field `{rail, proof, nonce}` (`tx` remains an alias). Solana rails and x402 header handling are still in design (`MULTICHAIN_PLAN.md` этап 2).

## Reference implementations

- Agent side: [`sidecar/`](sidecar/) (Python)
- Client side: [`frontend/`](frontend/) (browser, TON Connect), [`mcp/`](mcp/) (LLM tooling)
- Transport helper: [`ssl-gateway/`](ssl-gateway/) (HTTPS fronting for plain-HTTP agents)
