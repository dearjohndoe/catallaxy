from mcp.server.fastmcp import FastMCP

CONTENT = """# Sidecar Environment Variables

## Auto-injected (don't put these in .env)

| Variable | Source | Purpose |
|----------|--------|---------|
| SIDECAR_PYTHON | sidecar's sys.executable | Path to the sidecar venv's Python interpreter. Use it in AGENT_COMMAND=$SIDECAR_PYTHON agent.py — the agent runs under the same Python as the sidecar and inherits every pip package from its venv. Without it `python` may resolve to the system one, missing dependencies. |

## Required

| Variable | Description | Example |
|----------|-------------|---------|
| AGENT_COMMAND | Command to launch the agent. Leave $SIDECAR_PYTHON alone — substituted automatically. | $SIDECAR_PYTHON agent.py |
| AGENT_CAPABILITY | Agent capability. Accepts a comma-separated list; the first entry is primary. | translate |
| AGENT_NAME | Marketplace name | Translator Agent |
| AGENT_DESCRIPTION | Description | Translates text using AI |
| AGENT_SKUS | What the agent sells. Format: `id:stock:ton=N:usd=M[, ...]`. At least one rail; all paid SKUs must share the same rail set. For dynamic pricing use `ton=dynamic`/`usd=dynamic` (or legacy `=0`) — the price comes from the agent's mode=prices. A SKU priced `free` has no rails and no on-chain payment. | default:infinite:ton=10000000:usd=1000000 |
| AGENT_ENDPOINT | Public HTTPS URL | https://my-agent.example.com |
| TON_WALLET_PK | Wallet private key (hex). Legacy name `AGENT_WALLET_PK` still works as a fallback. | 0xabcdef... |

> The registry address is hardcoded in the sidecar (`settings.REGISTRY_ADDRESS`).
> Bare `REGISTRY_ADDRESS` is **not** read. `TON_REGISTRY_ADDRESS` can override the
> built-in default, but you normally never set it.

> **Per-chain (TON_*) aliases.** Several TON vars accept a `TON_`-prefixed name
> with the unprefixed legacy name as fallback: `TON_WALLET_PK`/`AGENT_WALLET_PK`,
> `TON_WALLET_SEED`/`AGENT_WALLET_SEED`, `TON_TESTNET`/`TESTNET`,
> `TON_REFUND_FEE_NANOTON`/`REFUND_FEE_NANOTON`. Either spelling works.

## State and DB files (important)

The sidecar namespaces its own files by a slug derived from `AGENT_NAME`, so two
sidecars on the same host never collide:

| File | Path | Configurable? |
|------|------|---------------|
| State (sidecar_id, etc.) | `.sidecar_state.<slug>.json` | `SIDECAR_STATE_PATH` (optional override) |
| Processed TX + refund queue | `processed_txs.<slug>.db` | **No.** Auto from `AGENT_NAME`, no env read |
| Stock inventory | `stock.<slug>.db` | **No.** Auto from `AGENT_NAME`, no env read |

`SIDECAR_TX_DB_PATH` and `SIDECAR_STOCK_DB_PATH` **no longer exist** — don't set
them, no effect. Cross-agent DB isolation comes from distinct `AGENT_NAME`s.

## Optional

| Variable | Default | Description |
|----------|---------|-------------|
| SIDECAR_STATE_PATH | .sidecar_state.&lt;slug&gt;.json | State file path (slug from AGENT_NAME) |
| PORT | 8080 | HTTP server port |
| PAYMENT_TIMEOUT | 300 | Payment nonce TTL (s) |
| AGENT_SYNC_TIMEOUT | 30 | Timeout before switching to async |
| AGENT_FINAL_TIMEOUT | 1200 | Max execution time |
| JOBS_TTL_SECONDS | 3600 | How long results are kept |
| TESTNET | false | Use testnet (alias TON_TESTNET) |
| AGENT_SKU_TITLES | — | Human-readable SKU names: `id1=Title 1,id2=Title 2` |
| AGENT_HAS_QUOTE | false | Support the /quote endpoint (dynamic price) |
| ENFORCE_COMMENT_NONCE | true | Require the nonce in the TX comment |
| REFUND_FEE_NANOTON | 500000 | Refund gas (alias TON_REFUND_FEE_NANOTON) |
| REFUND_WORKER_INTERVAL_SECONDS | 60 | Refund retry-worker interval |
| REFUND_MAX_ATTEMPTS | 10 | Max refund retry attempts |
| RATE_LIMIT_REQUESTS | 60 | Requests allowed per window |
| RATE_LIMIT_WINDOW_SECONDS | 60 | Rate-limit window |
| FREE_CLAIM_LIMIT | 1 | Max free-SKU claims per client IP per window |
| FREE_CLAIM_WINDOW_SECONDS | 2592000 | Free-claim window (30 days) |
| FILE_STORE_DIR | file_store | File storage directory |
| FILE_STORE_TTL | 900 | File TTL (s) |
| IMAGES_DIR | images | Directory served at `GET /images/{name}` for cover/gallery images |

## Media (optional)

Cover image, avatar and gallery — only set if you want them. PNG/JPEG/GIF/WebP only;
each URL ≤ 512 chars; total heartbeat payload ≤ 2 KB (extra media is dropped silently).

| Variable | Description |
|----------|-------------|
| AGENT_AVATAR_URL | Small avatar, surfaces as `avatar_url` |
| AGENT_PREVIEW_URL | Larger cover/preview, surfaces as `preview_url` |
| AGENT_IMAGES | Comma-separated gallery, up to 5 URLs, surfaces as `images` |

## Resilience / liteserver fallback (optional)

All optional — the defaults work for ordinary deploys.

| Variable | Default | Description |
|----------|---------|-------------|
| TONAPI_KEY | — | tonapi.io token. Used by the monitor as an HTTP fallback when ADNL (LiteBalancer) fails. Without a key TonAPI rate-limits to ~1 RPS per IP — on a multi-agent host the shared IP is tight, so set a key. The same key may be used by agent code (see below). |
| TONAPI_BASE | https://tonapi.io | TonAPI base URL. Change only for self-hosting/proxy. |
| TONAPI_FALLBACK_DISABLED | 0 | `=1` disables the TonAPI fallback in monitors. Only ADNL/LiteBalancer remain. |
| BALANCER_REBUILD_INTERVAL_SEC | 3600 | Interval (s) for background rebuilds of the verifiers'/sender's `LiteBalancer`. Cheap insurance against accumulating balancer state. ±15% jitter applied. |
| BALANCER_REBUILD_DISABLED | 0 | `=1` disables periodic balancer rebuilds. |
| PAYMENT_MONITOR_MAX_AGE_SEC | 60 | Seconds without a successful poll before a monitor is considered stale. If on preflight (no `tx_hash`) the monitor for the chosen rail is stale, the sidecar returns **503 Retry-After: 60** instead of 402, so the client never pays blind. |

`TONAPI_KEY` is now used by both agent code and the sidecar itself (in
WalletMonitor / JettonWalletMonitor) — set it in every .env that does payment polling.

## Owner Telegram bot (optional)

A per-agent Telegram bot that notifies the owner about payments (success, refund,
delayed refund from the worker). It starts **only if both** variables are set —
otherwise the sidecar starts normally, without the bot.

| Variable | Description | Example |
|----------|-------------|---------|
| TG_BOT_TOKEN | Bot token from @BotFather. Must be unique to this agent (description/replies are tied to `AGENT_NAME`). | 1234:ABC... |
| TG_USER_ID_LIST | Telegram user_id whitelist (comma-separated). Messages from others are ignored. Payment notifications go to every id in the list. | 123456789,987654321 |

If only one of the two is set, the sidecar refuses to start. To disable the bot,
remove both.

## Legacy fallback

`AGENT_PRICE` (nanoTON) and `AGENT_PRICE_USD` (micro-USDT) are honored only when
`AGENT_SKUS` is unset — a single SKU `default` is synthesized from these prices and
the optional `AGENT_STOCK`. For new agents, use `AGENT_SKUS`.
"""

def register_sidecar_env(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/sidecar-env")
    def sidecar_env() -> str:
        """All sidecar .env variables: required, optional, auto-injected (SIDECAR_PYTHON)."""
        return CONTENT
