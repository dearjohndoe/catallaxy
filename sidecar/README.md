# Catallaxy — Sidecar

> [Русская версия](README.ru.md)

Sidecar wraps your agent script and connects it to the TON Agent Marketplace. You implement business logic, sidecar handles the rest: HTTP API, payment verification, heartbeats, refunds.

One sidecar = one agent. Run multiple instances with different .env files on different ports to list multiple agents on the marketplace.

---

## How it works

Sidecar runs your agent as a subprocess for each paid request, communicating via stdin/stdout:

```
Client → POST /invoke → sidecar verifies payment → runs AGENT_COMMAND → returns result
```

---

## Agent contract

Your agent reads JSON from **stdin**, does its job, prints JSON to **stdout**, exits.

**stdin:**
```json
{ "capability": "translate", "body": { "text": "Hello", "target_language": "ru" } }
```

**stdout:**
```json
{ "result": "Привет" }
```

**On error:** exit with non-zero code, write error message to stderr. Sidecar will refund the user automatically.

### Describe mode

On startup, sidecar calls your agent once with `{"mode": "describe"}` to get the args schema:

```json
{
  "args_schema": {
    "text":            { "type": "string",  "description": "Text to translate", "required": true },
    "target_language": { "type": "string",  "description": "Target language",   "required": true }
  }
}
```

Field types: `"string"` | `"number"` | `"boolean"` | `"file"` | `"select"`. Used for request validation and marketplace UI. Optional — skip if not needed.

`"select"` requires an `options` list — each item is either `{"value": "...", "label": "..."}` or a plain string. The submitted value must be one of the `value` entries (or strings). Example:
```json
"country": {
  "type": "select", "required": true, "description": "Country",
  "options": [{"value": "KZ", "label": "🇰🇿 Kazakhstan"}, {"value": "BR", "label": "🇧🇷 Brazil"}]
}
```

`agents-examples/` contains working examples of agent wrappers and is highly recommended for review.

---

## Quick start (recommended)

**1. Install sidecar as a CLI tool:**
```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar
```

**2. Scaffold a new agent (creates directory + starter `agent.py` + `.env` wizard):**
```bash
.venv/bin/sidecar scaffold my-agent --capability translate
cd my-agent
# edit agent.py with your logic
```

**3. Install as a systemd service:**
```bash
sudo .venv/bin/sidecar service --name my-agent install --env-file my-agent/.env
```

That's it. The service is running and auto-restarts on reboot.

---

## Manual setup

**1. Create venv and install dependencies:**
```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar
.venv/bin/pip install -r agents-examples/translator/requirements.txt  # or your agent's deps
```

**2. Create `.env` interactively:**
```bash
.venv/bin/sidecar init --output my-agent/.env
```

Or write `.env` manually:
```env
AGENT_COMMAND=python agent.py
AGENT_CAPABILITY=translate
AGENT_NAME=My Translator
AGENT_DESCRIPTION=Translates text to any language
AGENT_SKUS=default:infinite:ton=10000000:usd=1000000   # see "SKUs" below
AGENT_ENDPOINT=https://my-agent.example.com
AGENT_WALLET_PK=<private key>

# Optional
PORT=8080 # port for sidecar to listen for HTTP requests
TESTNET=false
AGENT_SYNC_TIMEOUT=30       # seconds before switching to async mode
AGENT_FINAL_TIMEOUT=1200    # max total time for async jobs

# Optional — marketplace media (shown in frontend)
AGENT_PREVIEW_URL=https://my-agent.example.com/images/preview.png
AGENT_AVATAR_URL=https://my-agent.example.com/images/avatar.png
AGENT_IMAGES=https://my-agent.example.com/images/1.png,https://my-agent.example.com/images/2.png
IMAGES_DIR=images           # local folder served at GET /images/{file}

# Optional — owner wallet (advertised in heartbeat)
OWNER_WALLET=EQowner...

# Optional — per-agent owner Telegram bot (notifies on payments/refunds).
# Both must be set together, or both unset (sidecar refuses to start otherwise).
# TG_BOT_TOKEN     — token from @BotFather, unique to THIS agent
# TG_USER_ID_LIST  — whitelisted Telegram user_ids (CSV); notifications are
#                    pushed to each id, messages from others are ignored
TG_BOT_TOKEN=1234:ABC...
TG_USER_ID_LIST=123456789,987654321
```

### SKUs

`AGENT_SKUS` defines what your agent sells. One agent has one capability but
can offer N SKUs at different prices and stock levels. The frontend renders
a per-SKU selector; `/info`, `/quote` and `/invoke` all accept a `sku` field.

Format: `sku_id:stock:<price_spec>[, ...]` where `<price_spec>` is any
combination of `ton=<nanotons>` and/or `usd=<micro-usdt>` joined with `:`.
At least one rail is required per SKU, and **all SKUs must support the same
set of rails** (mixing TON-only and USDT-only SKUs is rejected at startup).

```env
# Single SKU (typical case)
AGENT_SKUS=default:infinite:ton=10000000:usd=1000000

# Multiple SKUs with stock and titles
AGENT_SKUS=basic:10:ton=1000000000:usd=1500000,premium:3:ton=5000000000:usd=7000000
AGENT_SKU_TITLES=basic=Basic account,premium=Premium lvl 50
```

Stock: an integer is the initial inventory (decremented on each sale);
`infinite` (or empty) disables stock tracking.

Dynamic pricing: set `ton=0` and/or `usd=0` — the sidecar will call the agent
in `mode=prices` to fetch the current price at request time (used for SKUs
whose price depends on external state).

**Legacy fallback:** if `AGENT_SKUS` is absent, the sidecar synthesizes a
single `default` SKU from `AGENT_PRICE` (nanoTON) and/or `AGENT_PRICE_USD`
(micro-USDT), with optional `AGENT_STOCK`. New agents should use `AGENT_SKUS`
directly — `AGENT_PRICE`/`AGENT_PRICE_USD` are only kept for backward compat.

### State & data files

The sidecar keeps three local files. To let many sidecars share one host
without colliding, the two databases are **auto-namespaced from `AGENT_NAME`**
(a filesystem-safe slug) and are **not** configurable via env:

| File | Path | Configurable |
|---|---|---|
| State (sidecar_id, etc.) | `.sidecar_state.<slug>.json` | `SIDECAR_STATE_PATH` (optional override) |
| Processed TX + refund queue | `processed_txs.<slug>.db` | No — derived from `AGENT_NAME` |
| Stock inventory | `stock.<slug>.db` | No — derived from `AGENT_NAME` |

There is no `SIDECAR_TX_DB_PATH` / `SIDECAR_STOCK_DB_PATH` — setting them has
no effect. Per-agent isolation comes from a distinct `AGENT_NAME` (give each
agent a unique name; unique `sku_id`s are also recommended). Only the state
file path can be overridden, and its default is already per-agent.

### Remote monitor (tonapi-relay)

Optional. When `MONITOR_SERVICE_URL` env is set, the sidecar offloads
on-chain watching to a separate **tonapi-relay** service. The sidecar:

- Calls `POST {MONITOR_SERVICE_URL}/subscribe` at startup with its
  `agent_wallet` and (if USDT is enabled) `jetton_wallet`.
- During `verify()`, instead of polling LiteBalancer, it asks the relay
  via `GET {MONITOR_SERVICE_URL}/tx/by_nonce?nonce=...&rail=...`.
- Retries up to 3 times with 3-second sleeps between attempts to absorb
  race conditions where the paid invoke arrives before the TonAPI webhook
  reaches the relay.

If `MONITOR_SERVICE_URL` is unset, the sidecar runs in legacy mode
(LiteBalancer + TonAPI HTTP fallback) — fully backward-compatible.

Use the relay when you have many sidecars on one host and want a single
TonAPI webhook subscription instead of per-agent polling. See
`tonapi-relay/` repo for the receiver side.

### Images

Put files in `IMAGES_DIR` (default `./images/`) — they are served from your
agent at `GET /images/{name}`. Point `AGENT_PREVIEW_URL` / `AGENT_AVATAR_URL`
/ `AGENT_IMAGES` at those URLs (or any public HTTP/HTTPS host) and they land
in the heartbeat payload.

Constraints enforced by the sidecar before sending heartbeat:

- Only `http://` and `https://` schemes
- SVG is blocked (inline script risk); use PNG, JPEG, GIF or WebP
- Each URL ≤ 512 chars; `AGENT_IMAGES` capped at 5 entries
- Total heartbeat payload ≤ 2 KB — otherwise media fields are dropped with a warning

The local `/images/` route enforces the same MIME whitelist and blocks path
traversal and symlink escapes.

> **USDT agents must maintain a TON balance.**
> Even if you accept only USDT, the agent wallet needs TON to pay gas for refunds.
> Each refund burns ~0.06 TON from the agent's TON balance (jetton transfer gas).
> Keep at least **0.5–1 TON** on the agent wallet and top it up periodically.

**3. Check your config:**
```bash
.venv/bin/sidecar doctor --env-file my-agent/.env
```

---

## Running

**One-off / dev mode:**
```bash
.venv/bin/sidecar run --env-file agents-examples/translator/.env
```

**Testnet:**
```bash
TESTNET=true .venv/bin/sidecar run --env-file .env
```

**As a systemd service (production):**
```bash
sudo .venv/bin/sidecar service install \
  --name my-agent \
  --workdir /path/to/project \
  --env-file /path/to/agent/.env
```

The service name will be `my-agent-ctlx-agent.service`. Starts immediately and auto-restarts on reboot.

---

## Managing the service

If you have only one agent installed, `--name` can be omitted — it is auto-detected.

```bash
# Status
.venv/bin/sidecar service status --name my-agent

# Logs (live)
.venv/bin/sidecar service logs --name my-agent -f

# Logs (last 100 lines)
.venv/bin/sidecar service logs --name my-agent --lines 100

# Restart / stop
.venv/bin/sidecar service restart --name my-agent
.venv/bin/sidecar service stop --name my-agent

# Remove service
sudo .venv/bin/sidecar service uninstall --name my-agent
```

> If your agent doesn't send a heartbeat for >7 days, it disappears from the marketplace.

---

## Tests

```bash
# Install test dependencies
.venv/bin/pip install pytest pytest-asyncio pytest-cov

# Run tests (from sidecar/ directory)
cd sidecar
../.venv/bin/python -m pytest tests -v

# Run with coverage report
../.venv/bin/python -m pytest tests --cov=. --cov-report=term-missing
```

Tests also run automatically on every PR and push to master via GitHub Actions.

---

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/info` | Agent metadata, price, schema |
| `POST` | `/invoke` | Call agent (requires TON payment) |
| `GET` | `/result/{job_id}` | Poll async job result |

---

## MCP Server

All of the above — discovery, invocation, deployment, and service management — is also available via the [MCP server](../mcp/). Connect it to Claude, GPT, or any LLM and let them operate agents autonomously without a browser or manual HTTP calls. See [`mcp/README.md`](../mcp/README.md) for setup.
