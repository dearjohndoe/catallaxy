---
name: catallaxy-agent
description: Build, deploy, and test agents on the Catallaxy marketplace (ctlx.cc). Covers project layout, agent.py contract, deploy pipeline, env conventions, known traps, and verification flow.
user-invocable: true
---

You are helping the user build a new agent for the Catallaxy decentralized 
marketplace (ctlx.cc) and ship it to the production sidecar host.
Reference agents already running: RugCheck, Wallet Story, DEX Compare,
Web Scraper, Video Summarizer, Premium-buyer, Stars-buyer, Image Gen,
LLM proxies (ChatGPT/Claude/Gemini), Telegram Channel Digest, Flight Search,
DNS Bind, Aged Groups.

Use `mcp__catallaxy__list_agents` to see the current live roster before
proposing anything new — the market changes.

### Host configuration (set before running any command here)

This skill never hard-codes the production host. Set it in the shell once:

```bash
export CATALLAXY_HOST=<your-sidecar-host-ip-or-domain>   # e.g. an IP or metrics.ctlx.cc
export CATALLAXY_SSH=root@$CATALLAXY_HOST
```

All `ssh`/`scp` snippets below use `$CATALLAXY_SSH` / `$CATALLAXY_HOST`.
If the user hasn't provided a host, ask for it — don't guess.

---

## 1. Server & access

- **Host:** `$CATALLAXY_SSH` (SSH keys forwarded). Ask the user for the IP
  or domain if `CATALLAXY_HOST` isn't set.
- Code root: `/root/agents/`
  - Agents: `/root/agents/agents-examples/<slug>/`
  - Env files: `/root/agents/test-agents/.env.<slug>`
  - Cover images: `/root/agents/images/`
  - Python venv: `/root/agents/.venv` (preinstalled `requests`, `python-dotenv`, etc.)
  - Sidecar entrypoint: `/root/agents/sidecar/sidecar.py`
- Sidecar systemd unit naming is **not strict**. Common patterns: `<slug>-ctlx-agent.service`
  (rugcheck, premium-buyer) and `<slug>-ask-agent.service` (LLM proxies:
  `claude-ask-agent`, `gemini-ask-agent`, `gpt-ask-agent`). Always grep first:
  `systemctl list-units --type=service | grep <slug>`.
- `OWNER_WALLET` and `AGENT_WALLET_PK`: **don't touch unless the user
  explicitly asks**. If you're rewriting an env, preserve the existing
  values; if a new agent needs them, ask which wallet to use rather than
  guessing.
- Sibling agents typically share the same `AGENT_WALLET_PK` — copy it from
  another `.env.*` on the server in the same shell session, never commit it
  and never echo it into a file that leaves the server.

## 2. Local project layout

Develop in a local working dir, e.g. `~/catallaxy/<slug>/`:
```
agent.py        # main entrypoint
.env.example    # documents required env vars (NO real secrets)
README.md       # English deployment notes
assets/<slug>.png   # cover image (square, ~512px) — kept OUT of the code dir
```

**Keep the cover image out of the agent code directory.** On the server the
sidecar serves only `IMAGES_DIR` (`/root/agents/images/`) at
`GET /images/{name}` — the agent code dir is *not* served. The deploy step
(§10) copies the image into that dedicated images dir, never alongside
`agent.py`. Don't ever point `IMAGES_DIR` at the code directory and don't
drop deployable images next to the code: separating served assets from
executable code is the safe default even though the served dir has a
MIME + path-traversal filter.

## 3. agent.py contract

Stdin/stdout JSON. The sidecar invokes `$SIDECAR_PYTHON agent.py` and pipes
one JSON request per call — each call is a fresh process, so no in-process
state survives. If the agent itself needs to persist state across calls, it
must manage its own file/DB inside its directory; the sidecar's own state
file and databases are not an agent storage API (see §6 for how the sidecar
namespaces them).

**Request shape:** `{"capability": "<cap>", "body": {<args>}}`

**Response shape depends on what the agent returns:**

Most agents return markdown text — no `mime_type`:
```python
print(json.dumps({"result": {"type": "string", "data": markdown_str}}))
```
Match `premium-buyer` / `stars-buyer` / `rugcheck` exactly. The user has corrected
extra-wrapped responses twice — keep it flat.

Binary results (images, audio, PDFs) use `type=file` and **do** include `mime_type`:
```python
print(json.dumps({"result": {
    "type": "file",
    "data": base64.b64encode(image_bytes).decode(),
    "mime_type": "image/png",
    "file_name": f"{uuid.uuid4()}.png",
}}))
```
For these, `RESULT_SCHEMA = {"type": "file", "mime_type": "image/png"}` in
`describe` mode. Reference: `agents-examples/imagegen/agent.py`.

**ARGS_SCHEMA — flat format (NOT JSON Schema):**
```python
ARGS_SCHEMA = {
    "param_name": {
        "type": "string",
        "description": "Plain English",
        "required": True,
    },
    # optional params: omit "required" or set False
}
RESULT_SCHEMA = {"type": "string"}
```
Do NOT wrap in `{"type": "object", "properties": {...}, "required": [...]}` —
that breaks the marketplace UI. This was a real correction from the user.

**Always look at a sibling agent before writing from scratch:**
- `/root/agents/agents-examples/ton-rugcheck/agent.py` — TON-data agent
- `/root/agents/agents-examples/premium-buyer/agent.py` — action agent (TG payment)

## 4. Markdown output

- Always use markdown. Don't return plain text or JSON unless the agent's job
  is genuinely structured data.
- Wrap TON addresses in tonviewer links:
  ```python
  TONVIEWER_BASE = "https://tonviewer.com"
  def addr_link(a, label=None):
      if not a: return "—"
      text = label if label is not None else short_addr(a)
      return f"[`{text}`]({TONVIEWER_BASE}/{a})"
  ```
- Use emojis sparingly in headers (matches RugCheck/Whale-style).
- Include a verdict / TL;DR at the top — users scan, don't read.

## 5. Logging (mirror premium-buyer)

```python
from pathlib import Path
import logging
_log_path = Path(__file__).parent / "agent.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(_log_path)],
)
log = logging.getLogger("<slug>")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
```
Logs land in `/root/agents/agents-examples/<slug>/agent.log` after deploy.
Tail with `ssh $CATALLAXY_SSH 'tail -f /root/agents/agents-examples/<slug>/agent.log'`.
Sidecar-level logs (heartbeat, payment, schema): `journalctl -u <unit-name> -n 50 -f`.

## 6. .env file template

Path on server: `/root/agents/test-agents/.env.<slug>`.

**⚠️ `AGENT_DESCRIPTION` MUST be in double quotes if it contains `\n`** —
without quotes the parser strips the backslash and the marketplace renders
`nGemini family` instead of `\nGemini family` (real incident: May 2026 LLM
agents). Siblings (`premium-buyer`, `rugcheck`, `stars-buyer`) all wrap it.

```
AGENT_COMMAND=$SIDECAR_PYTHON agents-examples/<slug>/agent.py
AGENT_CAPABILITY=ton                      # or "tools", "telegram", "llm", etc.
AGENT_NAME=My Agent 🎯
AGENT_DESCRIPTION="One-liner.\nSecond line. Third line."
AGENT_SKUS=<slug>-default:infinite:ton=27000000:usd=62000
AGENT_SKU_TITLES=<slug>-default=Default
AGENT_ENDPOINT=http://<host>:<port>
AGENT_WALLET_PK=<copy from sibling .env on server, or generate via scripts/gen_wallet.py — never commit>
AGENT_WALLET_SEED=<24-word mnemonic from the same source>
# Wallet address (reference): UQ...

# Keep PK + SEED + address together in the .env. The sidecar only reads PK, but:
# – SEED lets the owner import the wallet into Tonkeeper/MyTonWallet for
#   manual payouts, top-ups or recovery if the PK file is lost.
# – The address comment saves you from re-deriving it from PK every time you
#   need to fund the wallet or paste it somewhere.
# Skipping SEED is a real footgun — a few agents have already been re-deployed
# from scratch because the original key was unrecoverable.

# No REGISTRY_ADDRESS — it is hardcoded in the sidecar (settings.REGISTRY_ADDRESS),
# not an env var. Don't add it; setting it has no effect.
PORT=<unique port>
TESTNET=false
AGENT_HAS_QUOTE=false
IMAGES_DIR=/root/agents/images
OWNER_WALLET=<ask user — don't hard-code>
# TONAPI_KEY is read by BOTH the sidecar (HTTP fallback for the on-chain
# poller when liteservers misbehave — see catallaxy://spec/sidecar-env,
# "Resilience" section) AND any agent code that calls tonapi.io directly.
# Without it TonAPI rate-limits to ~1 RPS per IP, which on a multi-agent
# host is not enough. Put real values only in server-side .env.<slug>.
TONAPI_KEY=...
# Optional sidecar resilience knobs (all have sensible defaults):
# TONAPI_FALLBACK_DISABLED=1          # disable HTTP fallback entirely
# BALANCER_REBUILD_INTERVAL_SEC=14400 # period of periodic LiteBalancer rebuild
# BALANCER_REBUILD_DISABLED=1         # disable periodic rebuild
```

**State/DB files are auto-namespaced — do not set them.** The sidecar derives
its own files from a filesystem-safe slug of `AGENT_NAME`:

| File | Path | Configurable |
|---|---|---|
| State (sidecar_id, etc.) | `.sidecar_state.<slug>.json` | `SIDECAR_STATE_PATH` (optional override) |
| Processed TX + refund queue | `processed_txs.<slug>.db` | No |
| Stock inventory | `stock.<slug>.db` | No |

`SIDECAR_TX_DB_PATH` / `SIDECAR_STOCK_DB_PATH` **do not exist** — older env
files set them; they are dead now and can be deleted. Cross-agent isolation
comes from each agent having a distinct `AGENT_NAME`.

Cover image and gallery are optional — only add `AGENT_AVATAR_URL` /
`AGENT_PREVIEW_URL` / `AGENT_IMAGES` if the user asks. See §9 for details.

**Pick a free port:** `ssh $CATALLAXY_SSH 'ss -tlnp | grep -E ":66[0-9]{2}|:9[0-9]{3}"'`
to see what's taken.

### Multi-SKU agents (tiers)

A single agent can expose multiple price tiers via `AGENT_SKUS` (comma-
separated, **order cheapest → most expensive**):

```
AGENT_SKUS=gemini-lite:infinite:ton=4400000:usd=10000,gemini-flash:infinite:ton=8700000:usd=20000,gemini-pro:infinite:ton=17000000:usd=40000
AGENT_SKU_TITLES=gemini-lite=Gemini 3.1 Flash-Lite,gemini-flash=Gemini 3 Flash,gemini-pro=Gemini 3.1 Pro
```

The marketplace lets the user pick a SKU; the sidecar then passes
`sku` in the agent's invoke payload. Read it with
`task.get("sku") or body.get("sku")` and map to behavior (e.g. concrete
model name) via a dict in `agent.py`:

```python
SKU_MODEL_MAP = {
    "gemini-lite":  "gemini-3.1-flash-lite",
    "gemini-flash": "gemini-3.0-flash",
    "gemini-pro":   "gemini-3.1-pro",
}
```

You cannot pick the default SKU in code — the site decides which tier is
"primary" (typically the first / cheapest in the list).

### Product copy — writing AGENT_NAME / AGENT_DESCRIPTION

The name + description ARE the product card. Precise copy → fewer refunds
and disputes. Rules (from the FoxReload seller playbook + our own):

- **Be specific, not generic.** Bad: `Spotify 1 month`. Good:
  `Spotify Premium Brazil — 1 Month — Account Top-Up`. Bad: `Apple card`.
  Good: `Apple Gift Card Russia — 1000 RUB — RU Account Only`.
- **No dev jargon, no backend name unless it sells.** Drop "returns WAV as
  base64", "using Gemini AI". Keep "Claude", "Veo", "Imagen" — those are
  selling points the buyer searches for.
- **No placeholder names.** Never ship `My X Agent` / `X Agent`.
- **Description = 1 line of buyer benefit + what goes in / comes out.**
  Keep it tight; don't pad.

For **goods/codes/subscriptions** (not utility services), the description
should set expectations on every field that causes refunds:
- product **type** (key / top-up / account / subscription)
- **region / platform** restriction (e.g. "RU account only")
- **denomination** if a card
- **activation time** + how to redeem
- **refund policy** (when yes / no) + "save your TX hash"

Reference standard: the `Claude Code - Pro Gift Code` agent (list-seller
.env) — it nails type, redemption steps, restrictions, refund window and
support in a few tight lines. Copy its structure for any new paid code.

Utility/AI agents don't need region/denomination — apply the *spirit*
(precise, benefit-led, no jargon), e.g. `Translate text between 100+
languages. Paste text, choose the target language, get a clean instant
translation.`

## 7. Pricing parity (TON / USDT)

Catallaxy supports both TON and USDT rails — keep them at parity for the
current TON price.

**Always fetch the TON rate fresh** with `WebSearch "TON coin price USD"`
before computing SKUs. Never hard-code or recall a previous rate — TON
moves daily and the skill text will rot. Same for any API cost data (Gemini,
OpenAI, Claude pricing pages): re-fetch each time.

Format in `AGENT_SKUS`:
- `ton=27000000` → 0.027 TON (9 decimals)
- `usd=70000` → 0.07 USDT (6 decimals)

### Recipe: pricing an LLM-proxy agent

For agents that wrap an LLM API:

1. `WebSearch` current per-1M-token prices for each tier (input + output).
2. Assume **1000 input + 2048 output tokens** per call as the worst-case
   baseline. Cap output in the agent code with `max_output_tokens=2048` so
   the margin holds.
3. **Ask the user for the markup %** — they pick it per agent (30 %, 50 %,
   3×, etc.). Don't guess.
4. Round the final price up to the nearest USDT cent. Cents are the
   smallest the marketplace UI shows cleanly; the floor on tiny models is
   `$0.01`.
5. Convert USDT → TON using the **current** rate from step 0 (also
   `WebSearch`'d, not remembered).
6. Show the user the full table (model × cost × markup × rounded USDT × TON)
   **before** writing anything to env. Wait for sign-off on the numbers.

## 8. Stock SKU — use a unique sku_id

Each sidecar now keeps its **own** `stock.<slug>.db` (auto-namespaced from
`AGENT_NAME` — see §6), so the old cross-agent `stock.db` collision and the
`SIDECAR_STOCK_DB_PATH` workaround are **obsolete**. You no longer need to
add any DB-path env var; isolation is automatic per agent name.

Still do this: **give every SKU a unique, agent-prefixed id**
(`<slug>-default`, `gemini-lite`, …) rather than the bare `default`/`basic`.
Reasons that remain valid:
- Clear, self-describing SKU ids in the marketplace and in logs.
- If two agents are ever (mis)configured with the same `AGENT_NAME`, unique
  sku_ids are the second line of defence.
- Multi-SKU agents need distinct ids anyway.

Symptoms that used to indicate the old bug (wrong stock count, shared
`sold`/price across agents) should no longer occur with distinct
`AGENT_NAME`s. If you still see a stale stock count, check that the agent's
`AGENT_NAME` is unique and that no leftover `stock.db` (un-slugged, from an
old sidecar build) is present in `/root/agents/`.

### Frontend `sku_id` vs sidecar `sku` (was a real incident)

The `ctlx.cc` frontend used to send the multipart field `sku_id`, but the
sidecar reads `sku` (see `sidecar/api/http/multipart.py` and
`api/http/handlers/invoke.py`). On multi-SKU agents this surfaced as a 400
"sku is required (multiple SKUs configured)". Fixed in
`frontend/src/lib/agentClient.ts` in May 2026 — if it regresses, check
`buildMultipart` for `sku_id` instead of `sku`.

## 9. Media (opt-in)

**Don't add media unless the user asks** — many agents ship fine without
any image. When they do ask, three independent env keys are available
(any combination is fine):

- `AGENT_AVATAR_URL=...` — small avatar, surfaces as `avatar_url`
- `AGENT_PREVIEW_URL=...` — larger cover/preview, surfaces as `preview_url`
- `AGENT_IMAGES=url1,url2,url3,...` — gallery of up to **5** URLs,
  surfaces as `images`. Useful for image-gen agents that want to show
  sample outputs.

Constraints (from `sidecar/README.md`):
- PNG / JPEG / GIF / WebP only — SVG is blocked (inline-script risk)
- Each URL ≤ 512 chars; `AGENT_IMAGES` capped at 5
- Total heartbeat payload ≤ 2 KB — extra media fields are dropped silently
  with a warning if you exceed this

Files can live in `/root/agents/images/` (the sidecar serves `IMAGES_DIR`
at `GET /images/{name}` on its own port) or any public HTTP/HTTPS host.
Confirm with the user which to use and which image goes where.

## 9b. Owner Telegram bot (opt-in)

The sidecar can run a **per-agent** Telegram bot that notifies the owner
about payments, refunds, and delayed refunds from the refund worker. It is
fully optional — skip it unless the user asks.

**Always ask before adding it.** Sample question:
> «Подключить owner-бот для уведомлений о платежах? Нужен отдельный
> Telegram-бот (свой @BotFather-токен) для этого агента и список Telegram
> user_id владельца(ев).»

If yes, gather two values from the user (never invent):
- `TG_BOT_TOKEN` — token from @BotFather. **One bot per agent** — don't
  reuse a token between agents, the bot's description/replies are tied to
  `AGENT_NAME`.
- `TG_USER_ID_LIST` — comma-separated Telegram user_ids who can talk to
  the bot and who receive notifications.

Add them to `.env.<slug>` (server-side only — never in `.env.example`):

```
TG_BOT_TOKEN=1234:ABC...
TG_USER_ID_LIST=123456789,987654321
```

Both must be set together. Setting only one makes the sidecar refuse to
start (validated in `settings.load_settings`). To disable later, remove
both.

Env change → restart with `--force-heartbeat` like any other env edit (§10).

## 10. Deploy pipeline

After local testing:
```bash
# 1. Code + image
ssh $CATALLAXY_SSH 'mkdir -p /root/agents/agents-examples/<slug>'
# Code goes to the agent dir; the image goes to the SEPARATE served images
# dir — never into the agent code dir (see §2).
scp ./agent.py        $CATALLAXY_SSH:/root/agents/agents-examples/<slug>/agent.py
scp ./assets/<slug>.png $CATALLAXY_SSH:/root/agents/images/<slug>.png

# 2. Env (back up the old one first if updating)
ssh $CATALLAXY_SSH 'cp /root/agents/test-agents/.env.<slug> \
                      /root/agents/test-agents/.env.<slug>.bak.$(date +%s)'
scp ./.env.example $CATALLAXY_SSH:/root/agents/test-agents/.env.<slug>
# (then SSH in and fill secrets — never put real keys in .env.example)

# 3. systemd unit — two ways:
#    (a) Copy a sibling unit, sed-replace the env-file/exec paths, daemon-reload.
ssh $CATALLAXY_SSH 'cp /etc/systemd/system/rugcheck-ctlx-agent.service \
   /etc/systemd/system/<slug>-ctlx-agent.service'
ssh $CATALLAXY_SSH 'systemctl daemon-reload && systemctl enable --now <slug>-ctlx-agent'
#    (b) Let the sidecar generate it. Note: `service install` AUTO-APPENDS
#        `-ctlx-agent` to whatever you pass as `--name`, so pass the bare slug
#        (e.g. `--name foo` → installs `foo-ctlx-agent.service`).
ssh $CATALLAXY_SSH 'cd /root/agents && sudo .venv/bin/python sidecar/sidecar.py \
   service --name <slug> install --env-file test-agents/.env.<slug>'
```

Minor sidecar CLI quirks worth knowing:
- `service install` creates the unit and enables it but **does not start it**
  — follow with `systemctl start <unit>` or `service restart`.
- `service start` does **not** accept `--env-file` (only `restart`/`install` do).
- The auto-append behavior means `--name foo-agent` produces an awkward
  `foo-agent-ctlx-agent.service`. Pass the short slug.

### Restart with `--force-heartbeat` (REQUIRED for metadata changes)

`systemctl restart` alone is **not enough** when env metadata changes
(description, SKUs, prices, owner wallet, avatar). The sidecar caches the
last heartbeat and only re-sends on schedule, so the marketplace keeps
showing stale data until the next natural beat. Use the sidecar's own
restart command, which clears `last_heartbeat`:

```bash
ssh $CATALLAXY_SSH 'cd /root/agents && sudo .venv/bin/python sidecar/sidecar.py \
   service --name <unit-name> restart --env-file test-agents/.env.<slug> --force-heartbeat'
```

Run from `/root/agents/` (the working dir). One restart per unit if updating
multiple. Verify with `mcp__catallaxy__list_agents` after ~15 s — the
heartbeat needs to land + the marketplace needs to re-index.

### When restart IS / ISN'T needed

| Change | Restart? |
|---|---|
| `agent.py` body (request handling logic) | **No** — fresh process per invoke, sidecar reads file each time |
| `ARGS_SCHEMA` / `RESULT_SCHEMA` in `agent.py` | **Yes** — sidecar caches schema at startup via `describe` mode |
| Anything in `.env.<slug>` (description, SKUs, prices, wallet, image, port) | **Yes** + `--force-heartbeat` |
| Adding/removing a systemd unit | `systemctl daemon-reload` then enable/disable |

**Don't restart prod sidecars without asking** — even though it's fast, the
user has a strict preference for being told first.

## 11. Verification

### Local (before any deploy)
```bash
echo '{"capability":"<cap>","body":{"<param>":"<value>"}}' | \
   ./.venv/bin/python3 agent.py
```

### Production reachability
External APIs sometimes geoblock. Test from the actual server:
```bash
ssh $CATALLAXY_SSH 'curl -sS -m 10 https://api.example.com/test'
```

### Provider cost verification

If the user wants to sanity-check that real API spend matches the SKU pricing,
**a plain runtime API key is not enough** for either OpenAI or Google:

- OpenAI: `sk-proj-...` (project keys) get `insufficient permissions` on
  `/v1/organization/usage/*` and `/v1/organization/costs`. You'd need a
  separate `sk-admin-...` key with the `api.usage.read` scope. Without it,
  point the user to https://platform.openai.com/usage (browser-only).
- Google: a Gemini API key can't hit Cloud Billing API. Needs a
  service-account JSON with `roles/billing.viewer`. Without it, point to
  https://console.cloud.google.com/billing.

What you *can* do without those: count invocations from `agent.log`
(`vendor=X sku=Y model=Z` debug lines) or from `journalctl` HTTP-200 access
log entries, then multiply by the published per-call price. Be clear with
the user that this is a **calculation, not a real billing readout** —
discounts, credits, and caching can move the actual number.

### Marketplace
After the service is up, MCP-test it:
- `mcp__catallaxy__list_agents` — confirm it shows up alive
- `mcp__catallaxy__get_agent_info` with the endpoint — see its `/info`
- `mcp__catallaxy__test_agent` — invoke for free (dev/preview path)
- `mcp__catallaxy__agent_status` / `agent_logs` — runtime checks
- Marketplace pages:
  - `https://ctlx.cc/agent/<sidecar_id>`
  - `https://dearjohndoe.github.io/ton-agents-marketplace/agents/<sidecar_id>` (mirror)

## 12. MCP catallaxy tools (cheat sheet)

| Tool | When to use |
|------|-------------|
| `list_agents` | See what's live and avoid duplicate ideas |
| `get_agent_info` | Inspect schema/price of one agent |
| `scaffold_agent` | Bootstrap a new project skeleton |
| `validate_agent` | Schema/format check before deploy |
| `test_agent` | Free invoke for dev (no payment) |
| `deploy_agent` | Push to marketplace registry |
| `invoke_paid` | Real paid call (uses TON, costs money) |
| `get_quote` / `poll_result` | For agents with `AGENT_HAS_QUOTE=true` |
| `agent_status` / `agent_logs` / `ping_agent` | Live debug |
| `preflight` | Sanity check before going live |
| `stop_agent` | Kill a misbehaving agent — but see note below |

Heads-up on `stop_agent`: in May 2026 it returned `Access denied` against a
production systemd unit (the MCP runs as a non-root process and can't drive
`systemctl` on the host). Reliable fallback for already-deployed agents is
`ssh $CATALLAXY_SSH 'systemctl stop <unit>'`. Try `stop_agent` first if
the user prefers, but be ready to switch.

## 13. Pre-launch checklist

- [ ] `ARGS_SCHEMA` is flat (not JSON-Schema-wrapped)
- [ ] Result shape matches what the agent returns: `type=string` (no
      `mime_type`) for markdown, `type=file` (**with** `mime_type` + `data` as
      base64) for binary outputs — see §3
- [ ] All TON addresses wrapped in tonviewer links
- [ ] SKU id is unique to this agent (vendor/slug prefix, not `default`/`basic` — see §8)
- [ ] `AGENT_NAME` is unique across agents on the host (drives DB namespacing — §6)
- [ ] No `SIDECAR_TX_DB_PATH` / `SIDECAR_STOCK_DB_PATH` in the env (dead vars — §6)
- [ ] SKUs listed cheapest → most expensive in `AGENT_SKUS`
- [ ] `AGENT_DESCRIPTION` wrapped in double quotes if it contains `\n` (§6)
- [ ] `AGENT_NAME`/`AGENT_DESCRIPTION` follow product-copy rules — specific, no dev jargon, expectations set (§6)
- [ ] TON rate freshly fetched via WebSearch (not recalled) — §7
- [ ] Markup % confirmed with the user (no default)
- [ ] `OWNER_WALLET` confirmed with the user, or preserved from existing env
- [ ] `.env` contains all three: `AGENT_WALLET_PK`, `AGENT_WALLET_SEED`, and
      a comment with the wallet address. SEED is mandatory — without it the
      wallet is unrecoverable if the PK file is lost.
- [ ] Image URL only if the user asked for one (§9)
- [ ] Asked the user about owner Telegram bot (§9b); `TG_BOT_TOKEN` +
      `TG_USER_ID_LIST` either both set in `.env.<slug>` or both absent
- [ ] Logging block matches premium-buyer pattern (§5)
- [ ] External APIs reachable from the server (§11)
- [ ] Free port chosen, doesn't collide with siblings
- [ ] Local invoke returns markdown — eyeball the output
- [ ] User reviewed before `systemctl enable --now` / `service restart`
- [ ] Use `sidecar.py service ... --force-heartbeat` for restart (§10), not bare `systemctl restart`

## 14. Conventions for new ideas

When the user proposes a new agent, before drafting:
1. Run `list_agents` and check no one is doing the same thing
2. Check whether the data is already free elsewhere (Google search, public APIs).
   If yes, the agent must add real **action** or **synthesis**, not just info.
3. Estimate effort vs price: a single-vendor API wrapper at $0.05 per call
   needs to fight for volume; an action agent (deploy, snipe, mint) can charge
   $1-10 because it replaces hours of manual work.
4. Sketch ARGS_SCHEMA and a sample markdown response before writing code.
   Show the user, get sign-off on shape, then implement.

## 15. Style — what the user wants

- Russian replies (English when user writes English). NEVER Ukrainian.
- Concise — drop boilerplate, don't over-narrate.
- Don't restart prod services without asking. Show the exact restart command
  first; act after confirmation.
- Show price tables and diffs **before** writing them to env — the user wants
  to review the numbers, not see them post-hoc.
- Don't put placeholders/secrets in committed files. Real `*_API_KEY` /
  `*_WALLET_PK` values belong only in the server-side `.env.<slug>`, never in
  `.env.example` or anything that lands in git.
- Match the flat `ARGS_SCHEMA` pattern rigidly — the user corrected this
  twice. The `no mime_type` rule applies to text/markdown results;
  file-type results legitimately need a `mime_type`. Don't conflate the two.
- When rewriting an env, back it up first (`cp .env.<slug> .env.<slug>.bak.$(date +%s)`)
  and preserve secrets by grepping them out of the existing file in the same
  shell session — don't round-trip them through local disk.
