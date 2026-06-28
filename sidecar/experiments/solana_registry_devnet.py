#!/usr/bin/env python3
"""Devnet PoC: prove the CTLX discovery protocol works on Solana.

  keygen     — create the seller wallet (fund it so heartbeat can pay fees).
  heartbeat  — publish product.json as a thin on-chain listing:
               transfer 0.0001 SOL to the registry + Memo `CTLX:REG:<json>`.
  read       — scan the registry wallet, parse the memos, list the products.

Heavy fields (schemas, images) never touch the chain — the front/MCP fetch them
from the agent's endpoint. Devnet only; the registry wallet already exists.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

RPC_URL = "https://api.devnet.solana.com"
REGISTRY = Pubkey.from_string("8mJ49cRNj2zM1rvLXSBYAy65aSfJeS4KzeXo2jEuWYtR")  # marketplace wallet
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
REG_PREFIX = "CTLX:REG:"
HEARTBEAT_LAMPORTS = 100_000  # 0.0001 SOL marker that anchors the memo

HERE = Path(__file__).parent
SELLER_KEY = HERE / "seller.devnet.json"
PRODUCT_FILE = HERE / "product.json"


def load_seller() -> Keypair:
    if not SELLER_KEY.exists():
        sys.exit("no seller wallet — run: keygen")
    return Keypair.from_json(SELLER_KEY.read_text())


async def heartbeat() -> None:
    seller = load_seller()
    product = json.loads(PRODUCT_FILE.read_text())
    memo = REG_PREFIX + json.dumps(product, separators=(",", ":"), ensure_ascii=False)

    ixs = [
        transfer(TransferParams(
            from_pubkey=seller.pubkey(), to_pubkey=REGISTRY, lamports=HEARTBEAT_LAMPORTS)),
        Instruction(MEMO_PROGRAM, memo.encode(),
                    [AccountMeta(seller.pubkey(), is_signer=True, is_writable=False)]),
    ]
    async with AsyncClient(RPC_URL) as client:
        bh = (await client.get_latest_blockhash()).value.blockhash
        tx = Transaction([seller], Message.new_with_blockhash(ixs, seller.pubkey(), bh), bh)
        sig = (await client.send_raw_transaction(bytes(tx))).value
        await client.confirm_transaction(sig, commitment=Confirmed)
    print(f"published {product['sidecar_id']!r}: {sig}")
    print(f"  https://explorer.solana.com/tx/{sig}?cluster=devnet")


async def read_registry() -> None:
    products: dict[str, dict] = {}  # sidecar_id -> latest listing
    scanned = memos = 0
    async with AsyncClient(RPC_URL) as client:
        sigs = (await client.get_signatures_for_address(
            REGISTRY, limit=1000, commitment=Confirmed)).value
        for info in reversed(sigs):  # oldest-first so newer heartbeats win
            if info.err is not None:
                continue
            scanned += 1
            # base64 → real VersionedTransaction with raw `bytes` memo data
            # (default json encoding returns base58 strings instead).
            tx = (await client.get_transaction(
                info.signature, commitment=Confirmed,
                max_supported_transaction_version=0, encoding="base64")).value
            if tx is None:
                continue
            msg = tx.transaction.transaction.message
            for ci in msg.instructions:
                if msg.account_keys[ci.program_id_index] != MEMO_PROGRAM:
                    continue
                text = bytes(ci.data).decode("utf-8", "ignore")
                if not text.startswith(REG_PREFIX):
                    continue
                memos += 1
                try:
                    p = json.loads(text[len(REG_PREFIX):])
                    products[p["sidecar_id"]] = p
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

    print(f"scanned {scanned} tx, {memos} REG memos, {len(products)} unique products")
    for p in products.values():
        print(f"  {p.get('name','?')}  [{p['sidecar_id']}]  "
              f"rails={','.join(p.get('rails', []))}  price_hint={p.get('price_hint')}")
        print(f"    endpoint={p.get('endpoint','?')}")


def keygen() -> None:
    if SELLER_KEY.exists():
        sys.exit(f"{SELLER_KEY.name} already exists — refusing to overwrite")
    kp = Keypair()
    SELLER_KEY.write_text(kp.to_json())
    SELLER_KEY.chmod(0o600)
    print(f"seller wallet: {kp.pubkey()}")
    print("fund it on devnet (https://faucet.solana.com) so heartbeat can pay fees")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    if cmd == "keygen":
        keygen()
    elif cmd == "heartbeat":
        asyncio.run(heartbeat())
    elif cmd == "read":
        asyncio.run(read_registry())
    else:
        sys.exit("usage: solana_registry_devnet.py [keygen|heartbeat|read]")


if __name__ == "__main__":
    main()
