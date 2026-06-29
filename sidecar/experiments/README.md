# Catallaxy — Solana devnet experiment

Proof-of-concept of the Catallaxy on-chain **agent registry** on Solana (devnet).
A seller "heartbeats" by sending a tiny SOL transfer to the registry wallet with a
`CTLX:REG:` memo carrying the product JSON; clients read the registry by scanning
that wallet's transactions over RPC — no backend.

**Live on devnet** (real heartbeat txs on the marketplace wallet):
https://explorer.solana.com/address/8mJ49cRNj2zM1rvLXSBYAy65aSfJeS4KzeXo2jEuWYtR?cluster=devnet

**Run** (devnet · solana-py + solders):

`python solana_registry_devnet.py keygen`     # make a seller keypair

`python solana_registry_devnet.py heartbeat`   # register product.json on-chain

`python solana_registry_devnet.py read`        # scan + list the registry
