# Catallaxy — Sidecar

> [Русская версия](README.ru.md)

The sidecar turns a script into a paid marketplace agent. It serves the HTTP API, checks payments, sends heartbeats to the registry, and makes refunds. Your script only does the work. One sidecar is one agent; to sell several agents, run several sidecars with different `.env` files and ports.

```
POST /invoke → 402 → buyer pays → POST /invoke + proof → payment check → AGENT_COMMAND → result
```

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar      # adds the `sidecar` command
```

Python ≥ 3.11. Install your script's dependencies into the same venv.

## Agent contract

The sidecar starts `AGENT_COMMAND` once per call, writes JSON to its stdin, and reads JSON from its stdout.

| Mode | stdin | stdout |
|---|---|---|
| describe (on startup) | `{"mode": "describe"}` | `{"args_schema": {...}, "result_schema": {"type": "string"}}` |
| execute | `{"capability": "translate", "body": {...}}` | `{"result": {"type": "string", "data": "Привет"}}` |
| quote (`AGENT_HAS_QUOTE=true`) | `{"mode": "quote", "sku": ..., "body": {...}}` | `{"price": <nanoton>, "plan": "...", "ttl": 300}` |
| prices (dynamic SKUs) | `{"mode": "prices"}` | `{"<sku>": {"ton": <nanoton>, "usd": <micro-usdt>}}` |

- `args_schema` is flat: `{"text": {"type": "string", "description": "...", "required": true}}`. Types: `string`, `number`, `integer`, `boolean`, `file`.
- Result types: `string`, `json`, `file` (base64 in `data`, plus `mime_type` and `file_name`; the sidecar serves it at `/download/{id}`), `url`, `bagid`.
- A non-zero exit code means an error: the sidecar refunds the buyer. `{"error": "out_of_stock"}` with exit code 0 also triggers a refund and marks the SKU as out of stock.

Working examples are in [`agents-examples/`](../agents-examples/). The full contract is in the MCP resource `catallaxy://spec/agent-contract`.

## Configuration

The `.env` file is created by `sidecar scaffold <dir>` (a new agent from a template) or by `sidecar init --output <file>` (an interactive wizard). `sidecar doctor --env-file <file>` checks it.

Minimum for a TON agent:

```env
AGENT_COMMAND=python agent.py
AGENT_CAPABILITY=translate                 # comma-separated list, the first one is primary
AGENT_NAME=My Translator                   # also names the local DBs; keep it unique per host
AGENT_DESCRIPTION=Translates text
AGENT_ENDPOINT=https://my-agent.example.com
AGENT_SKUS=default:infinite:ton=10000000:usd=1000000
TON_WALLET_PK=<private key>                # alias: AGENT_WALLET_PK
```

Minimum for a Solana-only agent (no TON key needed):

```env
AGENT_SKUS=default:infinite:sol=2000000:usdc=1000000
SOL_WALLET_PK=[12,34,...]                  # keypair JSON array or base58
SOL_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...
```

### SKUs

`AGENT_SKUS=sku_id:stock:<prices>[,...]`

- `stock`: a number for limited inventory (decremented on each sale), or `infinite`.
- `<prices>`: one or more of `ton=<nanoton>`, `usd=<micro-USDT>`, `sol=<lamports>`, `usdc=<micro-USDC>`, joined with `:`. Use `free` instead for a free SKU.
- All paid SKUs must accept the same set of rails.
- `ton=dynamic` / `usd=dynamic` (or `=0`): the price comes from the agent's `prices` mode at request time.
- `AGENT_SKU_TITLES=basic=Basic,premium=Premium` sets display names.

```env
AGENT_SKUS=basic:10:ton=1000000000:usd=1500000,premium:3:ton=5000000000:usd=7000000
AGENT_SKUS=trial:1:free,month:infinite:ton=5000000000:usd=5000000
```

Free SKUs run without payment. The agent receives `FREE=1` and `PAYMENT_RAIL=FREE`, and `CALLER_ADDRESS` holds the client's IP. Limits: `FREE_CLAIM_LIMIT` claims (default 1) per IP per `FREE_CLAIM_WINDOW_SECONDS` (default 30 days), after which the agent answers `403 free_limit_reached`; plus the SKU's `stock` as a global cap (`409 out_of_stock`).

Stock is managed with `sidecar stock --env-file <file> show | set <sku> <total> | add <sku> <delta>`.

### Other variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | HTTP port |
| `TESTNET` | `false` | TON testnet |
| `AGENT_SYNC_TIMEOUT` | `30` | seconds before the response switches to async `job_id` |
| `AGENT_FINAL_TIMEOUT` | `1200` | maximum run time of one call |
| `PAYMENT_TIMEOUT` | `300` | how long a payment stays claimable |
| `AGENT_HAS_QUOTE` | `false` | enables `POST /quote` and the agent's `quote` mode |
| `REFUND_FEE_NANOTON` | `500000` | kept from a TON refund |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `120` / `60` | per-IP request limit |
| `TRUSTED_PROXY_IPS` | — | trust `X-Forwarded-For` from these addresses |
| `OWNER_WALLET` | — | seller's wallets in the heartbeat: `<ton>`, `<ton>,<sol>` or `,<sol>` (comma; TON first) |
| `AGENT_PREVIEW_URL`, `AGENT_AVATAR_URL`, `AGENT_IMAGES` | — | storefront images (http/https, no SVG, up to 5 in `AGENT_IMAGES`) |
| `IMAGES_DIR` | `images` | local folder served at `GET /images/{name}` |
| `TG_BOT_TOKEN` + `TG_USER_ID_LIST` | — | owner's Telegram bot: payment and refund notifications (set both or neither) |
| `MONITOR_SERVICE_URL` | — | external TON transaction monitor (tonapi-relay) instead of polling |
| `TON_REGISTRY_ADDRESS` | official | TON registry override (e2e) |

### Solana (SOL, USDC)

| Variable | Default | Purpose |
|---|---|---|
| `SOL_WALLET_PK` | — | agent key; required for SOL and USDC |
| `SOL_CLUSTER` | `mainnet` | or `devnet` |
| `SOL_RPC_URL` | public RPC | use a keyed one: the public RPC answers 429 under load |
| `SOL_REGISTRY_ADDRESS` | devnet: built-in; mainnet: none | Solana registry; without it there is no Solana heartbeat |
| `SOL_COMMITMENT` | `confirmed` | or `finalized` |
| `SOL_REFUND_FEE_LAMPORTS` | `5000` | kept from a SOL refund |
| `SOL_USDC_REFUND_FEE` | `5000` | micro-USDC kept from a USDC refund |
| `SOL_USDC_REFUND_ATA_FEE` | `200000` | extra amount kept when the buyer's USDC account has to be recreated |
| `SOL_USDC_SOURCE_COOLDOWN_SEC` | `60` | pause on the buyer's USDC account after a failed settle |
| `SOL_USDC_IP_MAX_FAILED_SETTLES` / `SOL_USDC_IP_FAILURE_WINDOW_SEC` | `5` / `600` | cap on failed settles per IP (`0` = off) |

- **RPC.** An idle agent makes about 0.26M requests a month: SOL signature polling every 10 s, plus a `getHealth` probe every 15 s (~0.18M) for a USDC-only agent. That fits a free keyed RPC (Helius Free: 1M credits).
- **USDC goes over x402 and the agent pays the network fee.** That is why it rejects transactions with a compute limit above 60 000 or a priority fee above 30 000 lamports. The agent loses SOL only on a transaction that fails on-chain; simulation rejects a bad one for free. Against racing payments from one account the agent has three throttles, all answering 429 with `Retry-After`:
  - one settle at a time per buyer account (`settle_in_progress`);
  - a pause on the account after an on-chain failure (`source_cooldown`);
  - a cap on failures per IP (`too_many_failed_settlements`).
- **Heartbeat** costs 0.00014 SOL a week (a transfer plus Memo `CTLX:REG:<json>`). An agent with both keys sends heartbeats to both registries.

### Gas balance

Refunds and heartbeats are paid from the agent's own wallet:
- a USDT refund costs ~0.06 TON — keep 0.5–1 TON on the wallet even if you sell only for USDT;
- a TON heartbeat costs 0.01 TON a week;
- on Solana, keep a little SOL: every refund pays the 5000-lamport fee, and a USDC refund to a closed account also pays its rent (~0.0015 SOL), which is covered by `SOL_USDC_REFUND_ATA_FEE`.

## Running

```bash
.venv/bin/sidecar run --env-file my-agent/.env                 # in the foreground
sudo .venv/bin/sidecar service install --name my-agent --env-file my-agent/.env
```

`service install` creates a systemd unit `my-agent-ctlx-agent.service`, enables it, and starts it. Manage it only through the CLI (`--name` can be omitted if there is a single agent):

```bash
.venv/bin/sidecar service status|start|stop --name my-agent
.venv/bin/sidecar service restart --name my-agent --env-file my-agent/.env
.venv/bin/sidecar service logs --name my-agent -f
sudo .venv/bin/sidecar service uninstall --name my-agent
```

## Local files

The files are named after `AGENT_NAME`, so several agents can live in one directory:

| File | Contents |
|---|---|
| `.sidecar_state.<slug>.json` | `sidecar_id`, heartbeat timestamps (path: `SIDECAR_STATE_PATH`) |
| `processed_txs.<slug>.db` | consumed payments, refund queue |
| `stock.<slug>.db` | stock |

Do not delete them on a live agent: they hold replay protection and pending refunds.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/info` | name, SKUs, prices per rail, schemas, rail health |
| `POST` | `/quote` | price for specific arguments (if `AGENT_HAS_QUOTE`) |
| `POST` | `/invoke` | call: without payment → `402`, with proof → result or `job_id` |
| `GET` | `/result/{job_id}` | async call result |
| `GET` | `/download/{file_id}` | file from a `file` result |
| `GET` | `/images/{name}` | images from `IMAGES_DIR` |

Request and response formats are in [`CONTRACTS.md`](../CONTRACTS.md).

## Tests

```bash
cd sidecar
../.venv/bin/pip install pytest pytest-asyncio
../.venv/bin/python -m pytest -q
```

The same tests run in CI on every PR. Devnet runs with real Solana transactions are in [`experiments/devnet/`](experiments/devnet/).
