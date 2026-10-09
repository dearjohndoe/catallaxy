"""Solana program ids, USDC mints, x402 CAIP-2 networks."""

from __future__ import annotations

from solders.pubkey import Pubkey

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
LIGHTHOUSE_PROGRAM = Pubkey.from_string("L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

USDC_MINT_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_MINT_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"

CAIP2_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
CAIP2_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

# x402 v1 network aliases still seen in X-PAYMENT payloads.
V1_NETWORK_MAINNET = frozenset({"solana", "solana-mainnet", "solana-mainnet-beta", CAIP2_MAINNET})
V1_NETWORK_DEVNET = frozenset({"solana-devnet", "solana:devnet", CAIP2_DEVNET})

TRANSFER_CHECKED_DISC = 12
CU_LIMIT_DISC = 2
CU_PRICE_DISC = 3
MAX_CU_PRICE_MICROLAMPORTS = 5
SETTLE_CACHE_TTL_SECONDS = 120
DEFAULT_RPC_MAINNET = "https://api.mainnet-beta.solana.com"
DEFAULT_RPC_DEVNET = "https://api.devnet.solana.com"

MEMO_PROGRAM_V1 = Pubkey.from_string("Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo")
# Native SOL rail: buyer's memo is ``CTLX:PAY:<pub_nonce>``; our refund memo is
# ``CTLX:REFUND:<json>``. ``sec`` never goes on-chain (split-nonce).
PAY_MEMO_PREFIX = "CTLX:PAY:"
REFUND_MEMO_PREFIX = "CTLX:REFUND:"
# Rent-exempt minimum for a 0-data system account (lamports). A transfer that
# leaves a new/drained account below this fails on-chain.
RENT_EXEMPT_MIN_LAMPORTS = 890_880
SIGNATURE_FEE_LAMPORTS = 5_000
