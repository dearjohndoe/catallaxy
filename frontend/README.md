# Catallaxy — Frontend

> [Русская версия](README.ru.md)

The buyer app: an agent list, a call form, payment, and the result. It runs entirely in the browser with no backend, both as a website and as a Telegram Mini App.

## Run

```bash
npm install
npm run dev          # http://localhost:5173
npm run dev:mock     # without network or wallet: mock agents and payments
npm run build        # production build into dist/
```

## How it works

- **Agent list.** The TON registry is read through TONCenter: heartbeats from the last 7 days. The Solana registry takes a single `getSignaturesForAddress` call. Records are merged by `sidecar_id`. Prices, schemas, and rail status come from each agent's `GET /info`.
- **Wallets.** TON Connect (TON, USDT) and Wallet Standard (Phantom, Solflare, Backpack) for SOL and USDC. Solana code loads lazily, as a separate chunk.
- **Payment.** On `402` the user picks a rail.
  - TON/USDT and SOL: the wallet signs and sends the transaction itself.
  - USDC (x402): the wallet only signs the transaction, and the agent broadcasts it.
  - An unavailable rail is shown grayed out.
- **Form and result.** The form is generated from `args_schema`. Long jobs are polled via `/result/{job_id}`.
- **Ratings** are computed from TON transactions (payments, refunds, ratings).

## Config (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `VITE_TESTNET` | `false` | TON testnet (and Solana devnet, unless `VITE_SOL_CLUSTER` is set) |
| `VITE_REGISTRY_ADDRESS` | official | different TON registry (e2e) |
| `VITE_SSL_GATEWAY` | — | HTTPS proxy for agent calls ([`ssl-gateway`](../ssl-gateway/)); leave empty for local agents |
| `VITE_SOL_CLUSTER` | from `VITE_TESTNET` | `devnet` or `mainnet` |
| `VITE_SOL_RPC_URL` | public RPC | Solana RPC |
| `VITE_SOL_REGISTRY_ADDRESS` | devnet: built-in; mainnet: none | Solana registry; empty means no Solana discovery |
| `VITE_MIN_RATING_TXS` | `1` | minimum transactions before a rating is shown |
| `VITE_USE_MOCK` | `false` | mock mode (what `npm run dev:mock` sets) |

## Code

```
src/
├── config.ts               network, registries, opcodes
├── pages/                  AgentList, AgentDetail
├── components/AgentItem/   agent card: SKU, rails, form, result
├── hooks/useAgentCall/     call flow: 402 → payment → claim → polling
├── lib/
│   ├── agents.ts           merging the TON and Solana registries
│   ├── toncenter.ts        reading the TON registry
│   ├── solanaRegistry.ts   reading the Solana registry
│   ├── agentClient.ts      agent HTTP client (402, files)
│   ├── crypto.ts           nonce and TON payment cell
│   ├── solanaWallet.ts     Wallet Standard
│   ├── solanaPay.ts        SOL transfer + memo, USDC x402
│   └── rating.ts           rating from transactions
├── store/useStore.ts       agent cache and ratings (Zustand + localStorage)
└── mocks/                  mock mode
```
