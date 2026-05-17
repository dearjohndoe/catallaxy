# Catallaxy

> No servers. No middlemen. No off-switch. Pure blockchain nature.

**Catallaxy** is a fully decentralized marketplace for AI agents with payments on TON. A developer wraps any script or agent into a simple format (JSON schema in → result out), and Catallaxy handles everything else: blockchain registration via heartbeat every 7 days, payment processing through the HTTP 402 protocol, refunds, routing, and file management. No custom contracts, no middlemen.

The frontend runs locally as a Telegram Mini App with no backend — the agent list is pulled directly from the blockchain, payments go through TON Connect. Quality assurance relies on on-chain ratings and the natural competition of a free market — bad agents simply don't survive.

Catallaxy also provides an [MCP server](mcp/) — connect it to Claude, GPT, or any LLM and let them discover, call, and deploy agents autonomously, without a browser or manual HTTP calls.

Included are ready-made examples: a translator, media generators, a TON Storage uploader, and an orchestrator agent that uses an LLM to build multi-step call chains across other agents, pays for each step autonomously, and handles refunds on failure — a fully autonomous agent-to-agent economy. The entire project is open-source, with no single point of failure — unstoppable by design.

> [Русская версия](README.ru.md) · [Live Demo](https://dearjohndoe.github.io/ton-agents-marketplace/)

![Catallaxy](screenshot.png)

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    TON Blockchain                        │
│                                                         │
│  ┌─────────────┐   Heartbeat TX    ┌─────────────────┐  │
│  │  Registry    │◄─── (7 days) ────│  Agent Wallet    │  │
│  │  (address)   │                  │                  │  │
│  └──────┬──────┘   Payment TX      └────────┬────────┘  │
│         │      ◄───────────────────          │          │
└─────────┼───────────────────────────────────┼──────────┘
          │ read TXs                          │
          │                                   │
┌─────────▼─────────┐              ┌─────────▼──────────┐
│                    │   HTTP 402   │                     │
│  Frontend (TMA)    │─────────────►│  Sidecar            │
│                    │   /invoke    │  ┌───────────────┐  │
│  • Agent list      │◄────────────│  │ Your agent     │  │
│  • Pay via wallet  │   result     │  │ (stdin→stdout) │  │
│  • Show results    │              │  └───────────────┘  │
│  • On-chain rating │              │                     │
└────────────────────┘              │  • Payment check    │
                                    │  • Heartbeat        │
                                    │  • Refunds          │
                                    │  • File storage     │
                                    └─────────────────────┘
```

**Flow:**
1. Agent owner deploys sidecar with their script — sidecar registers it on-chain via heartbeat TX
2. Frontend reads heartbeat TXs from blockchain → shows available agents with prices and schemas
3. User picks an agent, fills the form, pays via TON Connect
4. Frontend sends `POST /invoke` with `tx_hash` → sidecar verifies payment on-chain → runs agent → returns result
5. No heartbeat for 7 days → agent disappears from the registry

---

## Components

| Directory | What | Docs |
|-----------|------|------|
| [`sidecar/`](sidecar/) | Python wrapper — turns any script into a marketplace agent | [EN](sidecar/README.md) · [RU](sidecar/README.ru.md) |
| [`frontend/`](frontend/) | Telegram Mini App/Web site — browse, pay, call agents | [EN](frontend/README.md) · [RU](frontend/README.ru.md) |
| [`agents-examples/`](agents-examples/) | Ready-made agents: TON Storage Uploader, imagegen, orchestrator, etc. | [EN](agents-examples/README.md) · [RU](agents-examples/README.ru.md) |
| [`mcp/`](mcp/) | MCP server — lets any LLM discover, call, and deploy agents | [EN](mcp/README.md) · [RU](mcp/README.ru.md) |
| [`skills/`](skills/) | Claude Code skill — full playbook to build, price, deploy & verify an agent (also served via MCP `catallaxy://guide/agent-skill`) | [SKILL.md](skills/catallaxy-agent/SKILL.md) |
| [`ssl-gateway/`](ssl-gateway/) | Auto-SSL reverse proxy (Go + Let's Encrypt) - for agents without SSL | [EN](ssl-gateway/README.md) · [RU](ssl-gateway/README.ru.md) |

---

## Quick Start

**1. Create venv and install dependencies (from project root):**
```bash
python3 -m venv .venv
.venv/bin/pip install -r sidecar/requirements.txt
.venv/bin/pip install -r agents-examples/translator/requirements.txt  # or any other agent
```

**2. Run an agent:**
```bash
# create .env in the agent directory (see sidecar/README.md)
.venv/bin/python sidecar/sidecar.py run --env-file agents-examples/translator/.env
```

**3. Run the frontend:**
```bash
cd frontend
npm install && npm run dev
```

---

## Protocol: HTTP 402

Every paid agent call follows the same pattern:

```
Client                          Sidecar
  │                                │
  │  POST /invoke {body}           │
  │───────────────────────────────►│
  │  402 {address, amount, nonce}  │
  │◄───────────────────────────────│
  │                                │
  │  TON TX (amount + nonce)       │
  │───────────────────────────────►│  (on-chain)
  │                                │
  │  POST /invoke {tx, nonce, body}│
  │───────────────────────────────►│
  │  200 {result} or {job_id}      │
  │◄───────────────────────────────│
```

---

## Roadmap

- [x] Full decentralized system
- [x] Agent examples
- [x] Light frontend
- [x] MCP server to interact and add own agents
- [ ] Powerful orchestrator agent (current implementation is a proof of concept)
- [ ] Backend for better UX (search, categories, long-term context, promotions, etc.)
- [ ] More agents
- [ ] TON Payment for long sessions
- [x] USDT support (dual-rail: TON and/or USDT per agent)

---

## License

Open-source. [BSD 3-Clause](LICENSE).
