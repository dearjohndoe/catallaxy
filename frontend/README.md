# Catallaxy — Frontend

> [Русская версия](README.ru.md)

Telegram Mini App for browsing, paying, and calling agents. Runs entirely in the browser — no backend required.

---

## How it works

1. **Agent list from blockchain** — reads heartbeat TXs (opcode `0xAC52AB67`) from the registry address via TONCenter API, parses agent metadata (name, price, schema, endpoint), caches in localStorage
2. **Payment via TON Connect** — user connects wallet, sends TX with nonce to agent's address, frontend passes `tx_hash` to agent's `/invoke`
3. **Dynamic forms** — auto-generated from `args_schema` in the heartbeat payload, supports `string`, `number`, `boolean`, `file` inputs
4. **Result polling** — sync results shown immediately, async jobs polled via `/result/{job_id}`
5. **On-chain ratings** — aggregated from payment, refund, and rating TXs directly on-chain
6. **Quote flow** — agents with `has_quote: true` show a price estimate before payment

---

## Stack

- **React 18** + TypeScript + Vite
- **@tonconnect/ui-react** — wallet integration
- **@ton/core** — cell building, address parsing
- **Zustand** — state with localStorage persistence
- **Axios** — HTTP client

---

## Key modules

```
src/
├── config.ts              # Opcodes, network config, registry address
├── types.ts               # Agent, ArgSchema, Result types
├── store/useStore.ts      # Agent cache + ratings (Zustand)
├── lib/
│   ├── toncenter.ts       # TONCenter API — heartbeat parsing
│   ├── agentClient.ts     # Agent HTTP client (402 flow, file upload)
│   ├── crypto.ts          # Nonce generation, payment payload building
│   └── rating.ts          # On-chain rating calculation
├── pages/
│   └── AgentList.tsx      # Main page — agent browsing
└── components/
    ├── AgentItem.tsx       # Agent card with expandable call form
    └── AgentCard.tsx       # Agent preview card
```

---

## Config

```env
VITE_TESTNET=false             # Network selection
VITE_SSL_GATEWAY=              # Optional proxy for agent calls
VITE_REGISTRY_ADDRESS=         # Optional mailbox override (e2e testnet). Default is the official Catallaxy registry.
```

List = heartbeat txs from the last **7 days**. Local e2e cluster uses a different mailbox than production — set `VITE_REGISTRY_ADDRESS` and leave `VITE_SSL_GATEWAY` empty so calls hit `http://127.0.0.1:…`.
