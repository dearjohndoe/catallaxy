"""Refund memo + on-chain dedup shared by the Solana rails (SOL, USDC)."""

from __future__ import annotations

import json

from chains.solana.constants import REFUND_MEMO_PREFIX
from chains.solana.rpc import SolanaRpc


def refund_memo(original_tx_hash: str, reason: str, sidecar_id: str) -> str:
    """Same JSON body as the TON refund cell, so one scanner reads both."""
    return REFUND_MEMO_PREFIX + json.dumps(
        {"tx": original_tx_hash, "reason": reason, "sidecar_id": sidecar_id},
        separators=(",", ":"),
    )


async def find_refund_signature(
    rpc: SolanaRpc, seller: str, original_tx_hash: str, *, commitment: str = "confirmed",
) -> str | None:
    """Signature of a refund we already sent for ``original_tx_hash``.

    Scans the seller's last 200 signatures (the seller signs every refund, so
    they show up there); an older refund on a busy address is missed.
    """
    needle = json.dumps(original_tx_hash)
    rows = await rpc.get_signatures_for_address(seller, limit=200, commitment=commitment)
    for row in rows:
        memo = row.get("memo")
        if row.get("err") is None and isinstance(memo, str) and REFUND_MEMO_PREFIX in memo and needle in memo:
            return row["signature"]
    return None
