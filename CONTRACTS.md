# Catallaxy — Contracts Reference (HTTP / on-chain / DB)

Grounded in the sidecar reference implementation. Line cites are current as of this tree.
Three contract families: HTTP API, on-chain transactions (TON cells), and the SQLite schema.

---

## 1. HTTP contracts

**Routes** (`sidecar/api/http/routes.py`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/invoke` | preflight (→402) **and** claim (with payment) — one endpoint, two modes |
| POST | `/quote` | dynamic price from the agent |
| GET | `/result/{job_id}` | fetch an async job result |
| GET | `/download/{file_id}` | download a file result |
| GET | `/images/{name}` | agent preview images |
| GET | `/info` | agent metadata (schemas, prices) |

### `POST /invoke` — preflight mode (no `tx`)
Response `402` (`_invoke_helpers.py:80`):
```json
{ "error": "Payment required",
  "payment_request": { …first option… },
  "payment_options": [ … ] }
```
plus headers `x-ton-pay-address`, `x-ton-pay-amount`, `x-ton-pay-nonce`.

**`payment_option`** — one per rail:
```json
// TON (rail_ton.py:65)
{ "rail":"TON", "address":"<agent_wallet>", "amount":"<nanoton>", "memo":"<nonce>", "sku":"<sku_id>" }
// USDT (rail_usdt.py:69) — same + token block
{ "rail":"USDT", "address":"<agent_wallet>", "amount":"<micro-usdt>", "memo":"<nonce>",
  "token":{"symbol":"USDT","master":"<jetton_master>","decimals":6}, "sku":"<sku_id>" }
```
`memo` = nonce = `{uuid16}:{sidecar_id}` — binds the payment to one order + one agent.

### `POST /invoke` — claim mode (`{tx, nonce, sku, body}`)
- `200 {"status":"done","result":{…}}` — synchronous, or
- `200 {"job_id":"<uuid4>","status":"pending"}` — async, or
- `409` (tx already used / OOS), `503` (monitor degraded, `Retry-After`), `402` (payment not found).

### `GET /result/{job_id}` (`handlers/result.py`)
`{"status":"done", …result…}` | `{"status":"pending|error","error"?}` | `404`.
`job_id` is a server-side `uuid4` (`jobs.py:31`), never on-chain — a capability URL handed only to the claimant.

### `POST /quote`
`{ "price":<int>, "price_usdt"?:<int>, "plan":…, "note"?:…, "ttl"?:<sec> }`.

### Agent result payload (what goes into `result`)
plain `{…}`; file → `{"type":"file","url":"/download/…","expires_in":…}`; OOS signal → `{"error":"out_of_stock","reason":…}`.

---

## 2. On-chain contracts (TON cells)

Every body = `uint32 opcode` + payload. Opcodes in `chains/ton/transfer.py`, `chains/ton/jetton.py`.

| Action | Opcode | To | Body |
|---|---|---|---|
| **Heartbeat** | `0xAC52AB67` | registry wallet, 0.01 TON | `snake_string(JSON descriptor)` |
| **Payment** | `0x50415900` (`"PAY\0"`) | agent wallet, ≥ amount | `snake_string(nonce)` |
| **Refund** | `0x52464E44` (`"RFND"`) | agent → payer | `snake_string(json{tx,reason,sidecar_id})` |
| **Rating** | `0x52617465` (`"Rate"`) | agent wallet | `score:(1-5)` + `sidecar:{id}` — *client-layer, NOT enforced by the sidecar* |

**Heartbeat descriptor** (`heartbeat.py`, ≤900 bytes, CTLX/2 thin):
`sidecar_id, name, endpoint, rails, price_hint` (+ `price` alias, optional `capabilities` /
`price_usdt` / `has_quote` / `owner_wallet`). Fat fields (`args_schema`, `result_schema`,
`description`, media) live on `GET /info`. Readers still accept CTLX/1 fat payloads.

**USDT (jetton, TEP-74)** — two opcodes:
- **`0x0F8A7EA5` jetton transfer** (buyer → own jetton wallet): `query_id(64)`, `amount(coins)`, `destination`,
  `response_dest`, `custom_payload`, `forward_ton_amount(coins ~0.07)`, **`forward_payload` = payment cell carrying the nonce**.
- **`0x7362D09C` transfer_notification** (received by the agent's jetton wallet): `query_id`, `amount`, `sender`,
  **`forward_payload` = nonce**. The verifier matches on this (`jetton_monitor.py`).

Refund fingerprint `(tx, sidecar_id)` is embedded in the refund body → dedup against double-refund (`find_existing_refund_tx`).

---

## 3. Database (SQLite, one file set per agent)

Two files: **`processed_txs.{slug}.db`** (tx dedup + refunds + free quota) and **`stock.{slug}.db`** (inventory). WAL + `busy_timeout`.

### `processed_txs.{slug}.db`
```sql
-- payment dedup. After verify: "{chain}:{on_chain_hash}". Also
-- "{chain}:pub:{pub}" so retries/refund-worker agree with the queue key.
-- Client-supplied tx/proof is never a storage key.
processed_txs ( tx_hash TEXT PRIMARY KEY, created_at TEXT )

-- refund queue; PK is "{chain}:pub:{pub}", not the client tx.
-- states pending→refunding→refunded/failed/processed
pending_refunds (
  tx_hash TEXT PRIMARY KEY, nonce, rail, sender, amount, sku_id,
  status DEFAULT 'pending', refund_tx, attempts DEFAULT 0, last_error,
  created_at, last_attempt_at, next_attempt_at, force_refund DEFAULT 0 )

-- free-tier quota, keyed by IP
free_claims ( ip, sku_id, claimed_at, job_id )
```

### `stock.{slug}.db`
```sql
-- SKU catalogue; total NULL = infinite; FREE SKUs have both prices NULL
skus ( sku_id TEXT PRIMARY KEY, title, price_ton, price_usd,
       total, sold DEFAULT 0, created_at, updated_at )

-- audit log of stock deltas
stock_ledger ( id PK AUTOINC, sku_id, delta, reason, job_id, ts )

-- soft reservations (held during execution); dedup by key
stock_reservations ( key TEXT PRIMARY KEY, sku_id, expires_at, job_id, created_at )
```

### In-memory (NOT in the DB — lost on restart)
- `JobStore` — `job_id → {status, result, error}` (`jobs.py`).
- `file_store` — `file_id → {path, mime, expires_at}`.

---

## Known sharp edges (be ready to speak to these)

- **Stock ceiling is not schema-enforced.** `skus.total/sold` has no `CHECK` and no conditional
  `UPDATE … WHERE total-sold>0`; the ceiling is held by an in-process `asyncio.Lock` (`stock.py`).
  Correct single-process; two processes on one `stock.db` can oversell. Oversell across the
  preflight→pay→claim window is *compensated by refund*, not prevented.
- **JobStore is in-memory.** A crash between `mark_processed` and job completion loses the job and
  does **not** enqueue a refund → silent loss. Durable job log is a v2 item.
- **Claim is a bearer secret, not a wallet signature.** v2 splits the nonce (`pub` on-chain,
  `sec` only in the 402 JSON). `{tx, pub}` is not enough to claim. This proves the claimant
  saw the 402, not that they hold the paying key. `/result/{job_id}` is a capability URL.
- **Rating is sybil-able.** No self-dealing discount, `MIN_RATING_TXS=1`, score over one RPC page.
- **Solana/x402 is a plan, not code.** Этап 1 (thin heartbeat, generic `{rail,proof,nonce}`,
  split-nonce) сел на TON-стороне. `chains/solana/` и разбор `X-PAYMENT` ещё нет.
