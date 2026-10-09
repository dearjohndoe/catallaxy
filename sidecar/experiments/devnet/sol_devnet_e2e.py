"""Devnet e2e for the native SOL rail against the real RPC.

usage: python sol_devnet_e2e.py <seller.json> <buyer.json>
Seller funds a fresh buyer, buyer pays with the CTLX memo, the rail verifies
(hint path and poller path), an underpaid payment is classified, the rail
refunds, dedup finds the refund, leftover buyer funds go back to the seller.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # sidecar/

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from chains.solana.constants import MEMO_PROGRAM
from chains.solana.rail_sol import SolRail
from chains.solana.rpc import SolanaRpc
from chains.solana.sol_monitor import parse_sol_payment
from payments.nonce import mint_nonce
from payments.types import UnderpaidPayment

RPC = "https://api.devnet.solana.com"
SID = "devnet-e2e-sidecar"
PRICE = 1_000_000  # 0.001 SOL


def load(path: str) -> Keypair:
    return Keypair.from_bytes(bytes(json.load(open(path))))


async def send(rpc: SolanaRpc, payer: Keypair, to, lamports: int, memo: str | None = None) -> str:
    ixs = [set_compute_unit_limit(100_000), set_compute_unit_price(1),
           transfer(TransferParams(from_pubkey=payer.pubkey(), to_pubkey=to, lamports=lamports))]
    if memo:
        ixs.append(Instruction(MEMO_PROGRAM, memo.encode(), [AccountMeta(payer.pubkey(), True, False)]))
    bh, _ = await rpc.get_latest_blockhash()
    tx = VersionedTransaction(MessageV0.try_compile(payer.pubkey(), ixs, [], Hash.from_string(bh)), [payer])
    return await rpc.send_transaction(base64.b64encode(bytes(tx)).decode())


async def confirmed(rpc: SolanaRpc, sig: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = await rpc.signature_status(sig)
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            assert st.get("err") is None, st
            return
        await asyncio.sleep(1)
    raise TimeoutError(sig)


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name, detail if detail != "" else "")
    if not ok:
        raise SystemExit(1)


async def main(seller_path: str, buyer_path: str) -> None:
    seller = load(seller_path)
    if os.path.exists(buyer_path):
        buyer = load(buyer_path)
    else:
        buyer = Keypair()
        json.dump(list(bytes(buyer)), open(buyer_path, "w"))
    rpc = SolanaRpc(RPC)
    print("seller", seller.pubkey(), "buyer", buyer.pubkey())

    fund = await send(rpc, seller, buyer.pubkey(), 20_000_000)
    await confirmed(rpc, fund)
    print("funded buyer", fund)

    rail = SolRail(keypair=seller, rpc=SolanaRpc(RPC), cluster="devnet",
                   get_sidecar_id=lambda: SID, payment_timeout_seconds=600)
    rail.VERIFY_TIMEOUT = 60
    await rail.start()
    check("monitor healthy after start", rail.monitor_healthy())

    # 1. full payment, verified via the claim's proof hint
    _, _, pub_nonce, _ = mint_nonce(SID)
    opt = rail.payment_option(PRICE, pub_nonce)
    pay = await send(rpc, buyer, seller.pubkey(), PRICE, opt["memo"])
    await confirmed(rpc, pay)
    raw = await rpc.get_transaction(pay)
    parsed = parse_sol_payment(raw, signature=pay, pay_to=str(seller.pubkey()))
    print("parsed real tx:", parsed)
    check("parse amount from balance delta", parsed.amount == PRICE, parsed.amount)
    check("parse sender", parsed.sender == str(buyer.pubkey()), parsed.sender)
    check("parse memo nonce", parsed.nonce == pub_nonce, parsed.nonce)
    v = await rail.verify(proof=pay, nonce=pub_nonce, min_amount=PRICE)
    check("verify via hint", v.tx_hash == pay and v.amount == PRICE and v.sender == str(buyer.pubkey()), v)

    # 2. same nonce via the poller only (no hint)
    rail.monitor.consume(pub_nonce)
    await rail.monitor.poll()
    check("poller indexed memo", rail.monitor.get(pub_nonce) == pay, rail.monitor.get(pub_nonce))
    v2 = await rail.verify(proof="", nonce=pub_nonce, min_amount=PRICE)
    check("verify via poller", v2.tx_hash == pay)
    check("recover_payment", await rail.recover_payment(pub_nonce) == (str(buyer.pubkey()), PRICE))

    # 3. underpaid payment is classified as refundable
    _, _, pub2, _ = mint_nonce(SID)
    under = await send(rpc, buyer, seller.pubkey(), PRICE // 2, rail.payment_option(PRICE, pub2)["memo"])
    await confirmed(rpc, under)
    try:
        await rail.verify(proof=under, nonce=pub2, min_amount=PRICE)
        check("underpaid rejected", False)
    except UnderpaidPayment as exc:
        check("underpaid -> UnderpaidPayment", exc.payment.tx_hash == under and exc.payment.amount == PRICE // 2)

    # 4. refund + dedup
    ok, err = await rail.check_refund_balance(PRICE)
    check("check_refund_balance", ok, err)
    before = await rpc.get_balance(str(buyer.pubkey()))
    orig = f"solana:pub:{pub_nonce.split(':')[0]}"
    refund = await rail.refund(str(buyer.pubkey()), PRICE, original_tx_hash=orig, reason="devnet_e2e")
    check("refund sent", bool(refund), refund)
    await confirmed(rpc, refund)
    after = await rpc.get_balance(str(buyer.pubkey()))
    check("buyer got amount - fee", after - before == PRICE - 5_000, after - before)
    found = None
    for _ in range(10):
        found = await rail.find_existing_refund(orig)
        if found:
            break
        await asyncio.sleep(2)
    check("find_existing_refund", found == refund, found)
    check("dedup misses other ids", await rail.find_existing_refund("solana:pub:00000000") is None)

    await rail.close()

    # return leftovers (keep 0 lamports: drain fully, fee paid by buyer)
    bal = await rpc.get_balance(str(buyer.pubkey()))
    back = await send(rpc, buyer, seller.pubkey(), bal - 5_001)  # 5000 sig + 1 priority
    await confirmed(rpc, back)
    print("returned", bal - 5_001, "lamports to seller", back)
    await rpc.close()
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
