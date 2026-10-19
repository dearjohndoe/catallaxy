"""Solana discovery registry: heartbeat = SOL transfer + Memo ``CTLX:REG:<json>``.

Same thin descriptor as the TON heartbeat (``HeartbeatManager._build_payload``).
The transfer to the registry wallet is what makes the tx show up in that
wallet's ``getSignaturesForAddress``; readers parse the memo straight from that
listing, no ``getTransaction`` per row.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from chains.solana.constants import MEMO_PROGRAM, REG_HEARTBEAT_LAMPORTS, REG_MEMO_PREFIX
from chains.solana.rpc import SolanaRpc

logger = logging.getLogger("sidecar")

# Hard limit of a serialized Solana transaction.
MAX_TX_BYTES = 1232

# Memo program cost grows with memo length (~350 CU/byte): a ~570-byte
# multi-rail descriptor already burns past the 200k default and fails with
# "exceeded CUs meter". 1.4M is the per-tx max; at 1 micro-lamport/CU the
# priority fee stays ~1 lamport.
HEARTBEAT_CU_LIMIT = 1_400_000


def reg_memo(payload: dict[str, Any]) -> str:
    return REG_MEMO_PREFIX + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def split_rendered_memos(field: Any) -> list[str]:
    """Memos from the RPC's ``memo`` field (``[len] text`` joined by ``"; "``).

    ``len`` is the byte length, so a memo whose JSON contains ``"; "`` still
    splits correctly. Unparseable input comes back as one raw memo.
    """
    if not isinstance(field, str) or not field:
        return []
    raw = field.encode("utf-8")
    out: list[str] = []
    pos = 0
    while pos < len(raw):
        if raw[pos:pos + 1] != b"[":
            return out + [raw[pos:].decode("utf-8", "replace")]
        close = raw.find(b"] ", pos)
        if close < 0 or not raw[pos + 1:close].isdigit():
            return out + [raw[pos:].decode("utf-8", "replace")]
        n = int(raw[pos + 1:close])
        start = close + 2
        out.append(raw[start:start + n].decode("utf-8", "replace"))
        pos = start + n
        if raw[pos:pos + 2] == b"; ":
            pos += 2
    return out


def parse_reg_memo(field: Any) -> dict[str, Any] | None:
    """Heartbeat payload from a ``memo`` field, or None if there is none."""
    for memo in split_rendered_memos(field):
        if not memo.startswith(REG_MEMO_PREFIX):
            continue
        try:
            payload = json.loads(memo[len(REG_MEMO_PREFIX):])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


async def read_registry(
    rpc: SolanaRpc, registry: str, *, limit: int = 1000, commitment: str = "confirmed",
) -> dict[str, dict[str, Any]]:
    """``sidecar_id`` -> latest listing (``payload``/``signature``/``block_time``)
    from the registry's last ``limit`` transactions."""
    rows = await rpc.get_signatures_for_address(registry, limit=limit, commitment=commitment)
    listings: dict[str, dict[str, Any]] = {}
    for row in reversed(rows):  # oldest first: a newer heartbeat overwrites
        if row.get("err") is not None:
            continue
        payload = parse_reg_memo(row.get("memo"))
        if payload is None or not isinstance(payload.get("sidecar_id"), str):
            continue
        listings[payload["sidecar_id"]] = {
            "payload": payload,
            "signature": row.get("signature"),
            "block_time": row.get("blockTime"),
        }
    return listings


class SolanaRegistry:
    """``ChainRegistry`` for Solana: publishes the heartbeat and waits until the
    tx is confirmed, so a dropped tx is retried instead of recorded as sent."""

    chain = "solana"
    CONFIRM_TIMEOUT = 60.0

    def __init__(
        self,
        *,
        keypair: Keypair,
        rpc: SolanaRpc,
        registry_address: str,
        lamports: int = REG_HEARTBEAT_LAMPORTS,
    ) -> None:
        self._kp = keypair
        self._rpc = rpc
        self.registry = Pubkey.from_string(registry_address)
        self._lamports = lamports

    def build_tx(self, payload: dict[str, Any], blockhash: str) -> VersionedTransaction:
        seller = self._kp.pubkey()
        ixs = [
            set_compute_unit_limit(HEARTBEAT_CU_LIMIT),
            set_compute_unit_price(1),
            transfer(TransferParams(from_pubkey=seller, to_pubkey=self.registry, lamports=self._lamports)),
            Instruction(MEMO_PROGRAM, reg_memo(payload).encode(), [AccountMeta(seller, True, False)]),
        ]
        msg = MessageV0.try_compile(seller, ixs, [], Hash.from_string(blockhash))
        return VersionedTransaction(msg, [self._kp])

    async def publish_heartbeat(self, payload: dict[str, Any]) -> str:
        blockhash, _ = await self._rpc.get_latest_blockhash()
        raw = bytes(self.build_tx(payload, blockhash))
        if len(raw) > MAX_TX_BYTES:
            raise ValueError(f"heartbeat tx is {len(raw)} bytes > {MAX_TX_BYTES}")
        sig = await self._rpc.send_transaction(base64.b64encode(raw).decode("ascii"))
        await self._wait_confirmed(sig)
        logger.info("Solana heartbeat confirmed: %s", sig)
        return sig

    async def _wait_confirmed(self, sig: str) -> None:
        deadline = time.monotonic() + self.CONFIRM_TIMEOUT
        while True:
            status = await self._rpc.signature_status(sig)
            if status is not None:
                if status.get("err") is not None:
                    raise RuntimeError(f"heartbeat tx {sig} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"heartbeat tx {sig} not confirmed in {self.CONFIRM_TIMEOUT:.0f}s")
            await asyncio.sleep(2)

    async def close(self) -> None:
        await self._rpc.close()
