"""Seller keypair parsing for SOL_WALLET_PK."""

from __future__ import annotations

import json

from solders.keypair import Keypair


def parse_solana_keypair(raw: str) -> Keypair:
    """Accept JSON secret-key array, hex (32 or 64 bytes), or base58 64-byte secret."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty SOL_WALLET_PK")
    if text.startswith("["):
        arr = json.loads(text)
        if not isinstance(arr, list) or len(arr) not in (32, 64):
            raise ValueError("SOL_WALLET_PK json array must be 32 or 64 bytes")
        blob = bytes(int(x) for x in arr)
        if len(blob) == 32:
            return Keypair.from_seed(blob)
        return Keypair.from_bytes(blob)
    if all(c in "0123456789abcdefABCDEF" for c in text.removeprefix("0x")):
        blob = bytes.fromhex(text.removeprefix("0x"))
        if len(blob) == 32:
            return Keypair.from_seed(blob)
        if len(blob) == 64:
            return Keypair.from_bytes(blob)
        raise ValueError("SOL_WALLET_PK hex must be 32 or 64 bytes")
    from solders.keypair import Keypair as KP
    try:
        return KP.from_base58_string(text)
    except Exception:
        pass
    raise ValueError("SOL_WALLET_PK must be json array, hex, or base58 secret")
