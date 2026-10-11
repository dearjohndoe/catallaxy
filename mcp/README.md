# Catallaxy — MCP Server

> [Русская версия](README.ru.md)

An MCP server for Claude, GPT, and other LLMs. Through it a model finds agents, pays them, and gets results, and can also build and deploy its own agent. The server holds no keys: for a paid call it returns a payment to sign, and the user's wallet signs and sends it.

## Install and connect

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.txt
```

Claude Code:

```bash
claude mcp add catallaxy -- /path/to/repo/.venv/bin/python /path/to/repo/mcp/server.py
```

Claude Desktop (`claude_desktop_config.json`) and other clients:

```json
{ "mcpServers": { "catallaxy": {
    "command": "/path/to/repo/.venv/bin/python",
    "args": ["/path/to/repo/mcp/server.py"] } } }
```

## Tools

| Group | Tools |
|---|---|
| Discovery | `list_agents` (both registries, filter by capability), `get_agent_info`, `ping_agent` |
| Calls | `get_quote`, `preflight` (what to pay and how), `invoke_paid`, `invoke_free`, `poll_result` |
| Development | `scaffold_agent`, `test_agent`, `validate_agent`, `deploy_agent` (systemd), `agent_status`, `agent_logs`, `stop_agent` |

Resources (reference for the model): `catallaxy://spec/agent-contract`, `spec/sidecar-env`, `spec/payment-protocol`, `spec/result-types`, `guide/create-agent`, `guide/gotchas`, `guide/agent-skill` (the same as [`skills/catallaxy-agent`](../skills/catallaxy-agent/SKILL.md)).

## Payment by rail

`preflight` and `invoke_paid` take `rail`.

| Rail | `preflight` returns | `invoke_paid` takes |
|---|---|---|
| `TON`, `USDT` | address, amount, nonce, a ready payment cell, a Tonkeeper link and QR | `tx_hash` of the sent transaction |
| `SOL` | address, amount, `memo` text, Solana Pay link and QR; with `user_address`, an unsigned transaction | signature of the sent transaction |
| `USDC` (x402) | with `user_address`: an unsigned transaction, fee payer is the agent | the **signed** transaction (base64); the agent broadcasts it |

If a rail is down on the agent, `preflight` lists it in `unavailable_rails`. When the agent briefly limits a USDC settle, it answers 429; the money is not charged.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CATALLAXY_TESTNET` | `false` | TON testnet and Solana devnet |
| `CATALLAXY_TONCENTER_API_KEY` | — | TONCenter key (higher limits) |
| `CATALLAXY_SOL_CLUSTER` | from `CATALLAXY_TESTNET` | `devnet` or `mainnet` |
| `CATALLAXY_SOL_RPC_URL` | public RPC | Solana RPC |
| `CATALLAXY_SOL_REGISTRY_ADDRESS` | devnet: built-in; mainnet: none | Solana registry; empty means Solana agents are not listed |
| `CATALLAXY_PROJECT_ROOT` | this repo | where `scaffold_agent` creates agents and where `deploy_agent` deploys from |
