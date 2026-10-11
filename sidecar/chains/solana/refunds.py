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


class RefundDedupInconclusive(Exception):
    """The scan could not rule out a prior refund: the worker must not resend."""


# Dedup scans newest-first back to the queue entry's creation time; the cap
# bounds RPC cost on a seller that has been flooded since (logged when hit).
DEDUP_PAGE_SIZE = 200
DEDUP_MAX_PAGES = 25
DEDUP_CLOCK_SLACK_SECONDS = 300


async def find_refund_signature(
    rpc: SolanaRpc,
    seller: str,
    original_tx_hash: str,
    *,
    commitment: str = "confirmed",
    since: float | None = None,
) -> str | None:
    """Signature of a refund we already sent for ``original_tx_hash``.

    The seller signs every refund, so it shows up in the seller's signatures.
    Pages back until a row older than ``since`` (the refund cannot predate its
    queue entry); without ``since`` only the newest page is checked. Raises
    ``RefundDedupInconclusive`` if the page cap is hit first.
    """
    needle = json.dumps(original_tx_hash)
    floor = None if since is None else since - DEDUP_CLOCK_SLACK_SECONDS
    before: str | None = None
    for _ in range(DEDUP_MAX_PAGES):
        rows = await rpc.get_signatures_for_address(
            seller, limit=DEDUP_PAGE_SIZE, before=before, commitment=commitment,
        )
        for row in rows:
            memo = row.get("memo")
            if row.get("err") is None and isinstance(memo, str) and REFUND_MEMO_PREFIX in memo and needle in memo:
                return row["signature"]
        if floor is None or len(rows) < DEDUP_PAGE_SIZE:
            return None
        oldest = rows[-1].get("blockTime")
        if isinstance(oldest, (int, float)) and oldest < floor:
            return None
        before = rows[-1]["signature"]
    raise RefundDedupInconclusive(
        f"{DEDUP_MAX_PAGES} pages of seller signatures scanned without reaching "
        f"{since} for {original_tx_hash}"
    )
