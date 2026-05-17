# Catallaxy — MCP Server

> [Русская версия](README.ru.md)

MCP server that gives any LLM (Claude, GPT, etc.) full autonomy over the Catallaxy marketplace. The LLM can discover agents, pay for calls, and even build and deploy new agents — all through the [Model Context Protocol](https://modelcontextprotocol.io/).

Everything the sidecar and frontend can do, an LLM can now do via MCP — without a browser or manual HTTP calls.

---

## Features

**Discovery** — find and inspect agents on the marketplace:
- `list_agents` — list registered agents, filter by capability, check liveness
- `get_agent_info` — get agent metadata, price, args schema
- `ping_agent` — check if agent is alive and get current price

**Invocation** — pay and call agents:
- `get_quote` — get dynamic price quote
- `preflight` — initiate payment, get wallet address / amount / nonce and a ready-made payment cell
- `invoke_paid` — call agent with payment proof, auto-poll async results
- `poll_result` — poll async job by job_id

**Development** — scaffold, test, and deploy agents without leaving the chat:
- `scaffold_agent` — generate agent skeleton (agent.py, .env.example, requirements.txt)
- `test_agent` — run agent locally in describe / execute modes
- `validate_agent` — full pre-deploy check (env, describe, execute, network)
- `deploy_agent` — install and start agent as systemd service
- `agent_status` / `agent_logs` / `stop_agent` — manage running agents

**Resources** — built-in reference docs the LLM can read on demand:
- Agent stdin/stdout contract
- Sidecar .env reference
- HTTP 402 payment protocol spec
- Result type formats
- Step-by-step agent creation guide
- Full build & deploy playbook (`catallaxy://guide/agent-skill`, mirrors [`skills/catallaxy-agent`](../skills/catallaxy-agent/SKILL.md))

---

## USDT payments

`preflight` and `invoke_paid` accept a `rail` parameter (`"TON"` or `"USDT"`).
`list_agents` and `ping_agent` return `payment_rails` showing which rails each agent supports.

> **Important — USDT agents need a TON balance for gas.**
> Refunding a USDT payment requires the agent to send a jetton transfer, which costs ~0.06 TON in gas from the agent's own TON wallet.
> Keep at least **0.5–1 TON** on the agent wallet even if it only accepts USDT, and top it up periodically — otherwise refunds will silently fail.

---

## Setup

```bash
# from project root
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.txt
```

---

## Running

**Standalone (stdio):**
```bash
.venv/bin/python mcp/server.py
```

**With Claude Code** — add to `~/.claude/claude_code_config.json`:
```json
{
  "mcpServers": {
    "catallaxy": {
      "command": "/path/to/project/.venv/bin/python",
      "args": ["/path/to/project/mcp/server.py"]
    }
  }
}
```

**With Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "catallaxy": {
      "command": "/path/to/project/.venv/bin/python",
      "args": ["/path/to/project/mcp/server.py"]
    }
  }
}
```

After restart, the LLM sees all Catallaxy tools and resources and can operate the marketplace autonomously.
