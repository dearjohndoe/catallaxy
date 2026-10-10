"""Solana side of the MCP: registry reads and unsigned payment transactions.

Mirror of ``lib/toncenter.py`` + ``lib/cell_builder.py``. Reuses the sidecar's
Solana modules (``chains/solana/``) so the wire format has one source of
truth. The MCP never holds keys: it returns unsigned transactions for the
caller's wallet to sign.
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
from typing import Any
from urllib.parse import quote

_SIDECAR = os.path.join(os.path.dirname(__file__), "..", "..", "sidecar")
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.token.associated import get_associated_token_address  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from chains.solana.constants import (  # noqa: E402
    DEFAULT_RPC_DEVNET,
    DEFAULT_RPC_MAINNET,
    MEMO_PROGRAM,
    REGISTRY_ADDRESS_DEVNET,
    TOKEN_PROGRAM,
    TRANSFER_CHECKED_DISC,
)
from chains.solana.registry import read_registry  # noqa: E402
from chains.solana.rpc import SolanaRpc  # noqa: E402
from chains.solana.x402 import cluster_caip2  # noqa: E402

USDC_DECIMALS = 6
WEEK_SECONDS = 7 * 24 * 3600


def get_cluster() -> str:
    """``CATALLAXY_SOL_CLUSTER``; defaults to devnet next to TON testnet."""
    raw = os.getenv("CATALLAXY_SOL_CLUSTER", "").strip().lower()
    if not raw:
        raw = "devnet" if os.getenv("CATALLAXY_TESTNET", "false").lower() == "true" else "mainnet"
    return "devnet" if raw == "devnet" else "mainnet"


def get_rpc_url() -> str:
    return os.getenv("CATALLAXY_SOL_RPC_URL", "").strip() or (
        DEFAULT_RPC_DEVNET if get_cluster() == "devnet" else DEFAULT_RPC_MAINNET
    )


def get_registry_address() -> str:
    """Devnet has a default registry; mainnet has none until it is set up."""
    return os.getenv("CATALLAXY_SOL_REGISTRY_ADDRESS", "").strip() or (
        REGISTRY_ADDRESS_DEVNET if get_cluster() == "devnet" else ""
    )


async def fetch_registry_agents(now: float | None = None) -> list[dict[str, Any]]:
    """Latest heartbeat per ``sidecar_id`` from the Solana registry (7 days),
    as ``{payload, signature, block_time}``. Empty when no registry is set."""
    import time

    registry = get_registry_address()
    if not registry:
        return []
    rpc = SolanaRpc(get_rpc_url())
    try:
        listings = await read_registry(rpc, registry)
    finally:
        await rpc.close()
    cutoff = (now or time.time()) - WEEK_SECONDS
    return [v for v in listings.values() if (v.get("block_time") or 0) >= cutoff]


def lamports_to_sol(n: int) -> str:
    return f"{n / 1e9:.9f}".rstrip("0").rstrip(".")


def micro_to_usdc(n: int) -> str:
    return f"{n / 1e6:.6f}".rstrip("0").rstrip(".")


def solana_pay_url(address: str, lamports: int, memo: str) -> str:
    """Solana Pay transfer request; ``memo`` makes the wallet add a Memo
    instruction with exactly this text — what the sidecar matches on."""
    return f"solana:{address}?amount={lamports_to_sol(lamports)}&memo={quote(memo, safe='')}"


def _unsigned(msg: MessageV0) -> str:
    sigs = [Signature.default()] * msg.header.num_required_signatures
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


async def _blockhash(rpc_url: str | None) -> Hash:
    rpc = SolanaRpc(rpc_url or get_rpc_url())
    try:
        bh, _ = await rpc.get_latest_blockhash()
    finally:
        await rpc.close()
    return Hash.from_string(bh)


async def build_sol_payment_tx(
    payer: str, to: str, lamports: int, memo: str, *, rpc_url: str | None = None,
) -> str:
    """Unsigned v0 tx (base64): transfer + Memo ``memo``; ``payer`` pays fees.
    Sign and broadcast it with the payer's wallet; the signature is the proof."""
    payer_pk = Pubkey.from_string(payer)
    ixs = [
        transfer(TransferParams(from_pubkey=payer_pk, to_pubkey=Pubkey.from_string(to), lamports=lamports)),
        Instruction(MEMO_PROGRAM, memo.encode(), [AccountMeta(payer_pk, True, False)]),
    ]
    return _unsigned(MessageV0.try_compile(payer_pk, ixs, [], await _blockhash(rpc_url)))


async def build_x402_usdc_tx(
    payer: str, accepted: dict[str, Any], *, rpc_url: str | None = None,
) -> str:
    """Unsigned x402 ``exact`` tx (base64) in the sidecar's whitelisted shape:
    CU limit, CU price, TransferChecked; fee payer = ``extra.feePayer``.

    The payer's wallet must only SIGN it (not send): the seller co-signs as fee
    payer and broadcasts at claim time.
    """
    fee_payer = (accepted.get("extra") or {}).get("feePayer")
    if not fee_payer:
        raise ValueError("x402 requirements carry no extra.feePayer")
    owner = Pubkey.from_string(payer)
    mint = Pubkey.from_string(accepted["asset"])
    amount = int(accepted.get("amount") or accepted.get("maxAmountRequired") or 0)
    src = get_associated_token_address(owner, mint, TOKEN_PROGRAM)
    dst = get_associated_token_address(Pubkey.from_string(accepted["payTo"]), mint, TOKEN_PROGRAM)
    ixs = [
        set_compute_unit_limit(20_000),  # x402 reference default; sidecar caps at 60k
        set_compute_unit_price(1),
        Instruction(
            TOKEN_PROGRAM,
            bytes([TRANSFER_CHECKED_DISC]) + struct.pack("<Q", amount) + bytes([USDC_DECIMALS]),
            [
                AccountMeta(src, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(dst, False, True),
                AccountMeta(owner, True, False),
            ],
        ),
    ]
    msg = MessageV0.try_compile(Pubkey.from_string(fee_payer), ixs, [], await _blockhash(rpc_url))
    return _unsigned(msg)


def x402_payment_header(signed_tx_b64: str, accepted: dict[str, Any] | None = None) -> str:
    """``PAYMENT-SIGNATURE`` (x402 v2) for a wallet-signed exact tx."""
    accepted = accepted or {"scheme": "exact", "network": cluster_caip2(get_cluster())}
    payload = {
        "x402Version": 2,
        "accepted": accepted,
        "payload": {"transaction": signed_tx_b64.strip()},
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
