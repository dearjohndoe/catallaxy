"""Devnet e2e through a running sidecar's HTTP /invoke (SOL + USDC/x402).

usage: python e2e_invoke.py <seller.json> <buyer.json> <sidecar_url>
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # sidecar/

import aiohttp
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM, TransferParams, transfer
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from chains.solana.constants import ASSOCIATED_TOKEN_PROGRAM, MEMO_PROGRAM, TOKEN_PROGRAM, USDC_REFUND_FEE
from chains.solana.rail_usdc import UsdcRail
from chains.solana.rpc import SolanaRpc

RPC = "https://api.devnet.solana.com"
USDC = Pubkey.from_string("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU")
NATIVE_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
TOKEN_ACCOUNT_RENT = 2_039_280
SOL_PRICE = 1_000_000
USDC_PRICE = 10_000

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    results.append((name, ok, str(detail)))
    print(("PASS " if ok else "FAIL ") + name, detail if detail != "" else "", flush=True)


def load(path: str) -> Keypair:
    return Keypair.from_bytes(bytes(json.load(open(path))))


async def send_ixs(rpc: SolanaRpc, payer: Keypair, ixs: list[Instruction], signers=None) -> str:
    bh, _ = await rpc.get_latest_blockhash()
    msg = MessageV0.try_compile(payer.pubkey(), ixs, [], Hash.from_string(bh))
    tx = VersionedTransaction(msg, signers or [payer])
    sig = await rpc.send_transaction(base64.b64encode(bytes(tx)).decode())
    await confirmed(rpc, sig)
    return sig


async def confirmed(rpc: SolanaRpc, sig: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = await rpc.signature_status(sig)
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err") is not None:
                raise RuntimeError(f"{sig} failed: {st['err']}")
            return
        await asyncio.sleep(1)
    raise TimeoutError(sig)


def memo_ix(signer: Pubkey, text: str) -> Instruction:
    return Instruction(MEMO_PROGRAM, text.encode(), [AccountMeta(signer, True, False)])


def cu() -> list[Instruction]:
    return [set_compute_unit_limit(100_000), set_compute_unit_price(1)]


async def token_balance(rpc: SolanaRpc, owner: Pubkey) -> int:
    ata = get_associated_token_address(owner, USDC, TOKEN_PROGRAM)
    return (await rpc.get_token_account_balance(str(ata))) or 0


async def wait_balance(rpc: SolanaRpc, read, target_pred, timeout: float = 120) -> int:
    deadline = time.time() + timeout
    val = await read()
    while time.time() < deadline and not target_pred(val):
        await asyncio.sleep(3)
        val = await read()
    return val


async def preflight(http: aiohttp.ClientSession, url: str, rail: str | None, text: str):
    body: dict = {"capability": "echo", "body": {"text": text}}
    if rail:
        body["rail"] = rail
    async with http.post(f"{url}/invoke", json=body) as r:
        return r.status, await r.json(), dict(r.headers)


async def claim(http, url, payload, headers=None):
    async with http.post(f"{url}/invoke", json=payload, headers=headers or {}) as r:
        return r.status, await r.json()


async def sol_preflight(http, url, text):
    st, body, _ = await preflight(http, url, "SOL", text)
    assert st == 402, (st, body)
    return next(o for o in body["payment_options"] if o["rail"] == "SOL")


async def main(seller_path: str, buyer_path: str, url: str) -> None:
    seller = load(seller_path)
    if os.path.exists(buyer_path):
        buyer = load(buyer_path)
    else:
        buyer = Keypair()
        json.dump(list(bytes(buyer)), open(buyer_path, "w"))
    rpc = SolanaRpc(RPC)
    bpk = buyer.pubkey()
    print("seller", seller.pubkey(), "buyer", bpk)

    # fund buyer: SOL + 0.05 USDC (seller creates the buyer ATA)
    await send_ixs(rpc, seller, cu() + [transfer(TransferParams(
        from_pubkey=seller.pubkey(), to_pubkey=bpk, lamports=50_000_000))])
    usdc_helper = UsdcRail(keypair=seller, rpc=rpc, cluster="devnet")
    fund_usdc = await usdc_helper._send_usdc(str(bpk), 50_000)
    await confirmed(rpc, fund_usdc)
    print("buyer funded", await rpc.get_balance(str(bpk)), "lamports,", await token_balance(rpc, bpk), "usdc")

    async with aiohttp.ClientSession() as http:
        # ── A. SOL happy path ─────────────────────────────────────────
        opt = await sol_preflight(http, url, "hello")
        check("A 402 SOL option", opt["amount"] == str(SOL_PRICE) and opt["address"] == str(seller.pubkey()), opt["memo"])
        pay = await send_ixs(rpc, buyer, cu() + [
            transfer(TransferParams(from_pubkey=bpk, to_pubkey=seller.pubkey(), lamports=SOL_PRICE)),
            memo_ix(bpk, opt["memo"])])
        st, body = await claim(http, url, {"capability": "echo", "rail": "SOL", "proof": pay,
                                           "nonce": opt["nonce"], "body": {"text": "hello"}})
        check("A SOL claim delivers", st == 200 and "echo:hello" in json.dumps(body), (st, body))
        st, body = await claim(http, url, {"capability": "echo", "rail": "SOL", "proof": pay,
                                           "nonce": opt["nonce"], "body": {"text": "hello"}})
        check("A SOL replay refused", st != 200, (st, body))

        # ── B. SOL paid, agent out_of_stock → immediate refund ────────
        opt = await sol_preflight(http, url, "oos")
        before = await rpc.get_balance(str(bpk))
        pay = await send_ixs(rpc, buyer, cu() + [
            transfer(TransferParams(from_pubkey=bpk, to_pubkey=seller.pubkey(), lamports=SOL_PRICE)),
            memo_ix(bpk, opt["memo"])])
        after_pay = await rpc.get_balance(str(bpk))
        st, body = await claim(http, url, {"capability": "echo", "rail": "SOL", "proof": pay,
                                           "nonce": opt["nonce"], "body": {"text": "oos"}})
        check("B SOL oos reports refund", st == 200 and "refund" in json.dumps(body), (st, body))
        got = await wait_balance(rpc, lambda: rpc.get_balance(str(bpk)), lambda v: v > after_pay)
        check("B SOL refund landed (amount - 5000)", got - after_pay == SOL_PRICE - 5_000, got - after_pay)

        # ── C. SOL underpaid → 402 refund_pending → worker refunds ────
        opt = await sol_preflight(http, url, "under")
        pay = await send_ixs(rpc, buyer, cu() + [
            transfer(TransferParams(from_pubkey=bpk, to_pubkey=seller.pubkey(), lamports=SOL_PRICE // 2)),
            memo_ix(bpk, opt["memo"])])
        after_pay = await rpc.get_balance(str(bpk))
        st, body = await claim(http, url, {"capability": "echo", "rail": "SOL", "proof": pay,
                                           "nonce": opt["nonce"], "body": {"text": "under"}})
        check("C SOL underpaid 402 refund_pending", st == 402 and body.get("refund_pending") is True, (st, body))
        got = await wait_balance(rpc, lambda: rpc.get_balance(str(bpk)), lambda v: v > after_pay)
        check("C worker refunded underpayment", got - after_pay == SOL_PRICE // 2 - 5_000, got - after_pay)

        # ── D. pay by closing a wSOL account (no transfer instruction) ─
        opt = await sol_preflight(http, url, "oos")
        wsol = get_associated_token_address(bpk, NATIVE_MINT, TOKEN_PROGRAM)
        await send_ixs(rpc, buyer, cu() + [
            Instruction(ASSOCIATED_TOKEN_PROGRAM, bytes([1]), [
                AccountMeta(bpk, True, True), AccountMeta(wsol, False, True),
                AccountMeta(bpk, False, False), AccountMeta(NATIVE_MINT, False, False),
                AccountMeta(SYSTEM_PROGRAM, False, False), AccountMeta(TOKEN_PROGRAM, False, False)]),
            transfer(TransferParams(from_pubkey=bpk, to_pubkey=wsol, lamports=SOL_PRICE)),
            Instruction(TOKEN_PROGRAM, bytes([17]), [AccountMeta(wsol, False, True)]),  # SyncNative
        ])
        pay = await send_ixs(rpc, buyer, cu() + [
            Instruction(TOKEN_PROGRAM, bytes([9]), [  # CloseAccount -> seller
                AccountMeta(wsol, False, True), AccountMeta(seller.pubkey(), False, True),
                AccountMeta(bpk, True, False)]),
            memo_ix(bpk, opt["memo"])])
        raw = await rpc.get_transaction(pay)
        top = [ix.get("parsed", {}).get("type") if isinstance(ix.get("parsed"), dict) else ix.get("program")
               for ix in raw["transaction"]["message"]["instructions"]]
        print("   wSOL pay tx top-level:", top)
        after_pay = await rpc.get_balance(str(bpk))
        st, body = await claim(http, url, {"capability": "echo", "rail": "SOL", "proof": pay,
                                           "nonce": opt["nonce"], "body": {"text": "oos"}})
        check("D wSOL close accepted (balance delta)", st == 200 and "refund" in json.dumps(body), (st, body))
        got = await wait_balance(rpc, lambda: rpc.get_balance(str(bpk)), lambda v: v > after_pay)
        check("D refund went to signer, not the closed wSOL account",
              got - after_pay == SOL_PRICE + await rpc.min_rent(165, fallback=0) - 5_000, got - after_pay)

        # ── E. USDC x402 happy path ───────────────────────────────────
        st, body, headers = await preflight(http, url, None, "hello")
        req_hdr = headers.get("PAYMENT-REQUIRED") or headers.get("Payment-Required")
        check("E 402 has PAYMENT-REQUIRED + accepts", st == 402 and bool(req_hdr) and body.get("accepts"), st)
        accepted = json.loads(base64.b64decode(req_hdr))["accepts"][0]
        print("   accepts[0]:", {k: accepted[k] for k in ("scheme", "network", "amount", "payTo", "asset") if k in accepted},
              accepted.get("extra"))

        async def x402_header(cu_price: int = 1) -> tuple[str, str]:
            fee_payer = Pubkey.from_string(accepted["extra"]["feePayer"])
            pay_to = Pubkey.from_string(accepted["payTo"])
            amount = int(accepted.get("amount") or accepted.get("maxAmountRequired"))
            src = get_associated_token_address(bpk, USDC, TOKEN_PROGRAM)
            dst = get_associated_token_address(pay_to, USDC, TOKEN_PROGRAM)
            ixs = [set_compute_unit_limit(50_000), set_compute_unit_price(cu_price),
                   Instruction(TOKEN_PROGRAM, bytes([12]) + struct.pack("<Q", amount) + bytes([6]), [
                       AccountMeta(src, False, True), AccountMeta(USDC, False, False),
                       AccountMeta(dst, False, True), AccountMeta(bpk, True, False)])]
            bh, _ = await rpc.get_latest_blockhash()
            msg = MessageV0.try_compile(fee_payer, ixs, [], Hash.from_string(bh))
            keys = list(msg.account_keys)
            sigs = [Signature.default()] * msg.header.num_required_signatures
            sigs[keys.index(bpk)] = buyer.sign_message(to_bytes_versioned(msg))
            tx = VersionedTransaction.populate(msg, sigs)
            payload = {"x402Version": 2, "accepted": accepted, "resource": {"url": url},
                       "payload": {"transaction": base64.b64encode(bytes(tx)).decode()}}
            return base64.b64encode(json.dumps(payload).encode()).decode(), str(sigs[keys.index(bpk)])

        usdc_before = await token_balance(rpc, bpk)
        hdr, _ = await x402_header()
        st, body = await claim(http, url, {"capability": "echo", "body": {"text": "hello"}},
                               headers={"PAYMENT-SIGNATURE": hdr})
        check("E USDC x402 claim delivers", st == 200 and "echo:hello" in json.dumps(body), (st, body))
        usdc_after = await wait_balance(rpc, lambda: token_balance(rpc, bpk), lambda v: v < usdc_before, 30)
        check("E buyer paid exactly the price, seller paid the fee", usdc_before - usdc_after == USDC_PRICE,
              usdc_before - usdc_after)
        st, body = await claim(http, url, {"capability": "echo", "body": {"text": "hello"}},
                               headers={"PAYMENT-SIGNATURE": hdr})
        check("E USDC replay of same payment refused", st != 200, (st, body))

        # ── F. USDC paid, agent out_of_stock → USDC refund ────────────
        hdr, _ = await x402_header()
        before = await token_balance(rpc, bpk)
        st, body = await claim(http, url, {"capability": "echo", "body": {"text": "oos"}},
                               headers={"X-PAYMENT": hdr})
        check("F USDC oos reports refund (v1 X-PAYMENT header)", st == 200 and "refund" in json.dumps(body), (st, body))
        # The refund keeps SOL_USDC_REFUND_FEE (default; devnet.env does not override it).
        want = before - USDC_REFUND_FEE
        got = await wait_balance(rpc, lambda: token_balance(rpc, bpk), lambda v: v >= want, 60)
        check("F USDC refund landed (amount - refund fee)", got == want, (want, got))

        # ── G. Two payloads from one USDC account at once → one settles, one 429 ──
        (h1, _), (h2, _) = await x402_header(1), await x402_header(2)
        before = await token_balance(rpc, bpk)
        r1, r2 = await asyncio.gather(*(
            claim(http, url, {"capability": "echo", "body": {"text": "hello"}},
                  headers={"PAYMENT-SIGNATURE": h}) for h in (h1, h2)
        ))
        codes = sorted([r1[0], r2[0]])
        throttled = r1 if r1[0] == 429 else r2
        check("G parallel same-source: one 200, one 429 settle_in_progress",
              codes == [200, 429] and throttled[1].get("error") == "settle_in_progress", (r1, r2))
        got = await wait_balance(rpc, lambda: token_balance(rpc, bpk), lambda v: v < before, 30)
        check("G only one payment charged", before - got == USDC_PRICE, before - got)

    # cleanup: buyer USDC + SOL back to seller
    left_usdc = await token_balance(rpc, bpk)
    if left_usdc:
        src = get_associated_token_address(bpk, USDC, TOKEN_PROGRAM)
        dst = get_associated_token_address(seller.pubkey(), USDC, TOKEN_PROGRAM)
        await send_ixs(rpc, buyer, cu() + [Instruction(
            TOKEN_PROGRAM, bytes([12]) + struct.pack("<Q", left_usdc) + bytes([6]), [
                AccountMeta(src, False, True), AccountMeta(USDC, False, False),
                AccountMeta(dst, False, True), AccountMeta(bpk, True, False)])])
    src = get_associated_token_address(bpk, USDC, TOKEN_PROGRAM)
    await send_ixs(rpc, buyer, cu() + [Instruction(TOKEN_PROGRAM, bytes([9]), [  # close ATA, rent -> seller
        AccountMeta(src, False, True), AccountMeta(seller.pubkey(), False, True), AccountMeta(bpk, True, False)])])
    bal = await rpc.get_balance(str(bpk))
    await send_ixs(rpc, buyer, cu() + [transfer(TransferParams(
        from_pubkey=bpk, to_pubkey=seller.pubkey(), lamports=bal - 5_001))])
    print("cleanup: returned", left_usdc, "usdc and", bal - 5_001, "lamports")
    await rpc.close()
    failed = [n for n, ok, _ in results if not ok]
    print("FAILED:" if failed else "ALL PASS", failed if failed else "")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
