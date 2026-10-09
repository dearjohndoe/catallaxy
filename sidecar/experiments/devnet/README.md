# Solana devnet e2e (SOL + USDC/x402 + Squads)

Real-network checks for `chains/solana/`. Not part of pytest; costs devnet SOL.

Keys: a funded devnet seller keypair (JSON byte array, e.g.
`../seller.devnet.json`) with SOL and devnet USDC (faucet.circle.com).
Buyer/member keys are generated fresh, saved next to the scripts
(`*.devnet.json`, git-ignored) and drained back to the seller at the end.

1. Rail only, no sidecar:
   `python sol_devnet_e2e.py <seller.json> buyer.devnet.json`
2. Through a running sidecar (`/invoke`):
   - `run/devnet.env`: `AGENT_COMMAND=<python> <abs path>/agent.py`,
     `AGENT_SKUS=basic:inf:sol=1000000:usdc=10000`, `SOL_WALLET_PK=<seller json>`,
     `SOL_CLUSTER=devnet`, `PORT=18731`, `AGENT_ENDPOINT=http://127.0.0.1:18731`,
     `REFUND_WORKER_INTERVAL_SECONDS=5`, `RATE_LIMIT_REQUESTS=100`, plus any
     testnet `TON_WALLET_PK` (startup waits ~100 s on the failing TON heartbeat).
   - `cd run && python ../../../sidecar.py run --env-file devnet.env`
   - `python e2e_invoke.py <seller.json> buyer.devnet.json http://127.0.0.1:18731`
   - `npm install && python e2e_squads.py <seller.json> member.devnet.json http://127.0.0.1:18731`

`agent.py` echoes `text`; `"oos"` returns `out_of_stock` so the sidecar refunds.
The public devnet RPC rate-limits `getTransaction` (429); expected, the poller resumes.
