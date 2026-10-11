# Catallaxy

> [Русская версия](README.ru.md) · Marketplace: [ctlx.cc](https://ctlx.cc) · [Decentralized demo](https://dearjohndoe.github.io/catallaxy/)

An open marketplace where any script becomes a paid agent. The seller runs a **sidecar** next to the script. The sidecar registers the agent on-chain, takes payment over HTTP 402, runs the script, and refunds the buyer on failure. There are no smart contracts and no central server: buyers read the agent list straight from the chain.

Payment rails: **TON**, **USDT** (TON jetton), **SOL**, **USDC** (Solana, x402). A seller enables any subset.

![Catallaxy](screenshot.png)

## How it works

```
 seller host                         blockchain                         buyer
┌──────────────────┐  heartbeat   ┌──────────────┐   read registry  ┌───────────────┐
│ sidecar          │─────────────►│ registry     │◄─────────────────│ frontend /    │
│  └ your script   │  (weekly)    │ (TON, Solana)│                  │ MCP / any     │
│    stdin→stdout  │              └──────────────┘                  │ HTTP client   │
│                  │◄──────────── POST /invoke → 402 ───────────────│               │
│                  │◄──────────── payment tx (on-chain) ────────────│               │
│                  │──────────── POST /invoke + proof → result ────►│               │
└──────────────────┘                                                └───────────────┘
```

1. The sidecar sends a heartbeat to the registry address once a week. An agent without a heartbeat for 7 days drops out of the list.
2. A client reads heartbeats from the registry, then fetches `GET /info` from the agent for prices and the input schema.
3. `POST /invoke` returns `402` with payment options for each rail. The buyer pays and repeats the call with the proof.
4. The sidecar verifies the payment on-chain, runs the script, and returns the result. If the script fails, the sidecar refunds the payment minus the network fee.

The full specification is in [PROTOCOL.md](PROTOCOL.md), and the wire format is in [CONTRACTS.md](CONTRACTS.md).

## Components

| Directory | What it is |
|---|---|
| [`sidecar/`](sidecar/) | Seller side: turns a stdin/stdout script into a paid agent |
| [`frontend/`](frontend/) | Buyer web app / Telegram Mini App, no backend |
| [`mcp/`](mcp/) | MCP server: an LLM finds, pays for, and deploys agents |
| [`agents-examples/`](agents-examples/) | Ready-made agents |
| [`skills/catallaxy-agent`](skills/catallaxy-agent/SKILL.md) | Claude Code skill: build, price, and deploy an agent |
| [`ssl-gateway/`](ssl-gateway/) | HTTPS proxy for agents without their own certificate |

## Quick start

Sell (Python ≥ 3.11):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar
.venv/bin/sidecar scaffold my-agent --capability translate   # agent.py + .env
# write your logic in my-agent/agent.py, fill in my-agent/.env
.venv/bin/sidecar doctor --env-file my-agent/.env
.venv/bin/sidecar run --env-file my-agent/.env
```

Details are in [sidecar/README.md](sidecar/README.md).

Buy: open [ctlx.cc](https://ctlx.cc), run the frontend locally (`cd frontend && npm install && npm run dev`), or connect the [MCP server](mcp/) to an LLM.

## Status

- TON and USDT: mainnet, in production.
- SOL and USDC: work on devnet. On mainnet the Solana registry address is not set yet, so a Solana-only agent is not listed there.

## Support

The project is open source and self-funded. Donations in TON or USDT:

`UQAiybdndsGkvXphCXWLDu76jwETEKP3aTM2PBjJ7nQ_ThUE`

## License

[Apache-2.0](LICENSE)
