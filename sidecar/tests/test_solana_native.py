"""Native SOL rail: memo index, verify-by-pub, refunds, settings, /invoke flow.

RPC is an in-memory fake that speaks the jsonParsed shapes of
``getSignaturesForAddress`` / ``getTransaction`` — no network.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from chains.base import ChainRail
from chains.solana.constants import (
    MEMO_PROGRAM,
    RENT_EXEMPT_MIN_LAMPORTS,
)
from chains.solana.rail_sol import SolRail, refund_memo
from chains.solana.rpc import SolanaRpcError
from chains.solana.sol_monitor import (
    SolanaPaymentMonitor,
    extract_pay_nonce,
    parse_sol_payment,
    pay_memo,
)
from payments.types import PaymentVerificationError, UnderpaidPayment
from settings import AgentSku, DEFAULT_SKU_ID

from tests.test_api import app_factory, _serve_app  # noqa: F401 (app_factory is a fixture)


SID = "sid-test"
# Live cluster values (Oct 2026), below the old hardcoded fallbacks.
LIVE_RENT_0 = 650_240
LIVE_RENT_165 = 1_488_440
PUB_NONCE = f"abcd1234:{SID}"


def _sig() -> str:
    """A fresh, well-formed base58 signature string."""
    return str(Keypair().sign_message(str(time.time_ns()).encode()))


def _transfer_ix(source: str, dest: str, lamports: int) -> dict[str, Any]:
    return {
        "program": "system",
        "programId": "11111111111111111111111111111111",
        "parsed": {"type": "transfer", "info": {
            "source": source, "destination": dest, "lamports": lamports,
        }},
    }


def _memo_ix(memo: str) -> dict[str, Any]:
    return {"program": "spl-memo", "programId": str(MEMO_PROGRAM), "parsed": memo}


def _parsed_tx(
    *,
    source: str,
    dest: str,
    lamports: int,
    memo: str | None,
    block_time: int | None = None,
    err: Any = None,
    extra: list[dict[str, Any]] | None = None,
    top_level_transfer: bool = True,
    inner: list[dict[str, Any]] | None = None,
    lamport_moves: dict[str, int] | None = None,
    fee_payer: str | None = None,
) -> dict[str, Any]:
    """jsonParsed tx whose pre/postBalances follow from its system transfers
    (top-level + ``inner`` CPIs) plus ``lamport_moves`` (direct mutation by an
    owning program). Fee payer (default ``source``) pays 5000; a failed tx
    only moves the fee."""
    ixs: list[dict[str, Any]] = []
    if top_level_transfer:
        ixs.append(_transfer_ix(source, dest, lamports))
    if memo is not None:
        ixs.append(_memo_ix(memo))
    ixs.extend(extra or [])
    payer = fee_payer or source
    deltas: dict[str, int] = {payer: -5_000}
    if err is None:
        for ix in ixs + (inner or []):
            parsed = ix.get("parsed")
            if ix.get("program") == "system" and isinstance(parsed, dict):
                info = parsed["info"]
                deltas[info["source"]] = deltas.get(info["source"], 0) - info["lamports"]
                deltas[info["destination"]] = deltas.get(info["destination"], 0) + info["lamports"]
        for k, v in (lamport_moves or {}).items():
            deltas[k] = deltas.get(k, 0) + v
    keys = [payer] + [k for k in deltas if k != payer]
    pre = [10**12] * len(keys)
    post = [b + deltas[k] for b, k in zip(pre, keys)]
    meta: dict[str, Any] = {"err": err, "preBalances": pre, "postBalances": post}
    if inner:
        meta["innerInstructions"] = [{"index": 0, "instructions": inner}]
    return {
        "blockTime": int(time.time()) if block_time is None else block_time,
        "meta": meta,
        "transaction": {"message": {
            "accountKeys": [{"pubkey": k, "signer": k == payer, "writable": True} for k in keys],
            "instructions": ixs,
        }},
    }


class FakeRpc:
    """Seller-address history (newest first) + transactions by signature."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.txs: dict[str, dict[str, Any]] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        self.balance = 10 * 10**9
        self.sent: list[str] = []
        self.fail_signatures = False
        self.fail_send = False
        self.signature_calls: list[dict[str, Any]] = []
        self.tx_calls: list[str] = []
        self.closed = False

    def pay(
        self, *, source: str, dest: str, lamports: int, memo: str | None,
        err: Any = None, block_time: int | None = None, indexed: bool = True,
        cpi_memo: str | None = None,
    ) -> str:
        """``cpi_memo``: memo only inside a CPI — RPC's signature row shows none."""
        sig = _sig()
        tx = _parsed_tx(
            source=source, dest=dest, lamports=lamports, memo=memo,
            err=err, block_time=block_time,
            inner=[_memo_ix(cpi_memo)] if cpi_memo else None,
        )
        self.txs[sig] = tx
        if indexed:
            rendered = f"[{len(memo)}] {memo}" if memo else None
            self.rows.insert(0, {
                "signature": sig, "err": err, "memo": rendered,
                "blockTime": tx["blockTime"],
            })
        return sig

    async def get_signatures_for_address(
        self, address, *, limit=100, before=None, until=None, commitment="confirmed",
    ):
        self.signature_calls.append({"before": before, "until": until, "limit": limit})
        if self.fail_signatures:
            raise SolanaRpcError("boom")
        rows = list(self.rows)
        if before is not None:
            idx = next(i for i, r in enumerate(rows) if r["signature"] == before)
            rows = rows[idx + 1:]
        if until is not None:
            cut = next((i for i, r in enumerate(rows) if r["signature"] == until), len(rows))
            rows = rows[:cut]
        return rows[:limit]

    async def get_transaction(self, signature, commitment="confirmed"):
        self.tx_calls.append(signature)
        return self.txs.get(signature)

    async def get_account_info(self, pubkey):
        return self.accounts.get(pubkey)

    async def get_balance(self, pubkey, commitment="confirmed"):
        return self.balance

    async def min_rent(self, data_len, *, fallback, ttl=3600.0):
        return {0: LIVE_RENT_0, 165: LIVE_RENT_165}[data_len]

    async def get_latest_blockhash(self):
        return "11111111111111111111111111111111", 100

    async def send_transaction(self, tx_b64):
        if self.fail_send:
            raise SolanaRpcError("send failed")
        self.sent.append(tx_b64)
        return _sig()

    async def close(self):
        self.closed = True


def _rail(rpc: FakeRpc | None = None, *, kp: Keypair | None = None, **kw: Any) -> SolRail:
    rpc = rpc or FakeRpc()
    rail = SolRail(
        keypair=kp or Keypair(),
        rpc=rpc,  # type: ignore[arg-type]
        cluster="devnet",
        get_sidecar_id=lambda: SID,
        payment_timeout_seconds=kw.pop("payment_timeout_seconds", 300),
        **kw,
    )
    rail.VERIFY_TIMEOUT = 0.05
    rail.VERIFY_POLL = 0.0
    return rail


def _decode_sent(b64: str) -> VersionedTransaction:
    return VersionedTransaction.from_bytes(base64.b64decode(b64))


# ── memo / tx parsing ───────────────────────────────────────────────────

def test_extract_pay_nonce_from_raw_and_rpc_rendered_memo():
    assert extract_pay_nonce(pay_memo(PUB_NONCE)) == PUB_NONCE
    assert extract_pay_nonce(f"[54] {pay_memo(PUB_NONCE)}") == PUB_NONCE
    assert extract_pay_nonce(f"[3] gm; [54] {pay_memo(PUB_NONCE)}") == PUB_NONCE
    assert extract_pay_nonce("[5] hello") is None
    assert extract_pay_nonce("CTLX:PAY:nothex12:sid") is None
    assert extract_pay_nonce(None) is None


def test_pay_memo_carries_only_pub_half():
    # 16-hex full nonce (pub+sec) must not be a valid pay memo match.
    assert extract_pay_nonce(pay_memo(f"abcd1234deadbeef:{SID}")) is None


def test_parse_sol_payment_sums_transfers_to_us_and_reads_memo():
    seller, buyer, other = (str(Keypair().pubkey()) for _ in range(3))
    tx = _parsed_tx(
        source=buyer, dest=seller, lamports=700, memo=pay_memo(PUB_NONCE),
        extra=[
            {"program": "system", "parsed": {"type": "transfer", "info": {
                "source": buyer, "destination": seller, "lamports": 300}}},
            {"program": "system", "parsed": {"type": "transfer", "info": {
                "source": buyer, "destination": other, "lamports": 10**9}}},
        ],
    )
    p = parse_sol_payment(tx, signature="S", pay_to=seller)
    assert (p.amount, p.sender, p.nonce, p.failed) == (1000, buyer, PUB_NONCE, False)


def test_parse_sol_payment_counts_cpi_transfer_from_smart_wallet():
    """Squads-style: member signs, vault PDA pays via CPI — no top-level transfer."""
    seller, member, vault = (str(Keypair().pubkey()) for _ in range(3))
    tx = _parsed_tx(
        source=member, dest=seller, lamports=0, memo=pay_memo(PUB_NONCE),
        top_level_transfer=False, inner=[_transfer_ix(vault, seller, 2_000_000)],
    )
    p = parse_sol_payment(tx, signature="S", pay_to=seller)
    assert (p.amount, p.sender, p.nonce) == (2_000_000, vault, PUB_NONCE)


def test_parse_sol_payment_counts_direct_lamport_move_without_transfer_ix():
    """E.g. closing a wSOL account to the seller: the program-owned account
    drops most, but the refund must go to the signer, not to that account."""
    seller, payer, program_acct = (str(Keypair().pubkey()) for _ in range(3))
    tx = _parsed_tx(
        source=payer, dest=seller, lamports=0, memo=pay_memo(PUB_NONCE),
        top_level_transfer=False,
        lamport_moves={program_acct: -3_000_000, seller: 3_000_000},
    )
    p = parse_sol_payment(tx, signature="S", pay_to=seller)
    assert (p.amount, p.sender) == (3_000_000, payer)


def test_parse_sol_payment_reads_memo_from_cpi():
    seller, buyer = str(Keypair().pubkey()), str(Keypair().pubkey())
    tx = _parsed_tx(source=buyer, dest=seller, lamports=10, memo=None,
                    inner=[_memo_ix(pay_memo(PUB_NONCE))])
    assert parse_sol_payment(tx, signature="S", pay_to=seller).nonce == PUB_NONCE


def test_parse_sol_payment_one_tx_pays_one_purchase():
    """Two CTLX:PAY memos, one transfer: the whole delta goes to the first memo
    only — the second purchase never sees a payment (no double credit)."""
    seller, buyer = str(Keypair().pubkey()), str(Keypair().pubkey())
    other = f"99999999:{SID}"
    tx = _parsed_tx(source=buyer, dest=seller, lamports=10, memo=pay_memo(PUB_NONCE),
                    extra=[_memo_ix(pay_memo(other))])
    p = parse_sol_payment(tx, signature="S", pay_to=seller)
    assert (p.nonce, p.amount) == (PUB_NONCE, 10)


def test_parse_sol_payment_flags_failed_tx():
    seller, buyer = str(Keypair().pubkey()), str(Keypair().pubkey())
    tx = _parsed_tx(source=buyer, dest=seller, lamports=5, memo=None,
                    err={"InstructionError": [0, "Custom"]})
    p = parse_sol_payment(tx, signature="S", pay_to=seller)
    assert p.failed and p.nonce is None and p.amount == 0


# ── monitor ─────────────────────────────────────────────────────────────

async def test_monitor_indexes_memo_and_reports_health():
    rpc = FakeRpc()
    seller = str(Keypair().pubkey())
    mon = SolanaPaymentMonitor(rpc, seller)  # type: ignore[arg-type]
    assert mon.is_healthy() is False
    sig = rpc.pay(source="B", dest=seller, lamports=1, memo=pay_memo(PUB_NONCE))
    await mon.poll()
    assert mon.get(PUB_NONCE) == sig
    assert mon.is_healthy() is True
    mon.consume(PUB_NONCE)
    assert mon.get(PUB_NONCE) is None


async def test_monitor_indexes_cpi_memo_by_fetching_memoless_rows():
    """Squads: memo lives in a CPI, the signature row has memo=None."""
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    sig = rpc.pay(source="vault", dest="S", lamports=5, memo=None, cpi_memo=pay_memo(PUB_NONCE))
    top = rpc.pay(source="B", dest="S", lamports=5, memo=pay_memo(f"99999999:{SID}"))
    await mon.poll()
    assert mon.get(PUB_NONCE) == sig
    assert rpc.tx_calls == [sig]  # rows with a top-level memo are not fetched
    assert mon.get(f"99999999:{SID}") == top


async def test_monitor_fetch_cap_resumes_next_poll_without_skipping():
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    mon.MAX_TX_FETCHES = 2
    sigs = [rpc.pay(source="v", dest="S", lamports=1, memo=None,
                    cpi_memo=pay_memo(f"0000000{i}:{SID}")) for i in range(5)]
    await mon.poll()
    assert len(rpc.tx_calls) == 2 and mon.is_healthy()
    assert [mon.get(f"0000000{i}:{SID}") for i in range(5)] == sigs[:2] + [None] * 3
    await mon.poll()
    await mon.poll()
    assert [mon.get(f"0000000{i}:{SID}") for i in range(5)] == sigs
    assert len(rpc.tx_calls) == 5  # each row fetched exactly once


async def test_monitor_rpc_error_on_fetch_retries_and_keeps_order():
    """429 on a CPI-memo row: poll fails (health not refreshed), progress up to
    that row is kept, and the row is retried — never silently skipped. A later
    dust copy of its memo cannot take the nonce meanwhile."""
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    first = rpc.pay(source="B", dest="S", lamports=1, memo=pay_memo(f"11111111:{SID}"))
    squads = rpc.pay(source="vault", dest="S", lamports=10, memo=None, cpi_memo=pay_memo(PUB_NONCE))
    rpc.pay(source="attacker", dest="S", lamports=1, memo=pay_memo(PUB_NONCE))
    real_get = rpc.get_transaction
    calls = {"n": 0}

    async def flaky(sig, commitment="confirmed"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SolanaRpcError("429")
        return await real_get(sig, commitment)

    rpc.get_transaction = flaky  # type: ignore[method-assign]
    with pytest.raises(SolanaRpcError):
        await mon.poll()
    assert mon.is_healthy() is False
    assert mon.get(f"11111111:{SID}") == first
    assert mon.get(PUB_NONCE) is None
    await mon.poll()
    assert mon.get(PUB_NONCE) == squads
    assert mon.is_healthy()


async def test_monitor_skips_failed_txs():
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    rpc.pay(source="B", dest="S", lamports=1, memo=pay_memo(PUB_NONCE), err={"x": 1})
    await mon.poll()
    assert mon.get(PUB_NONCE) is None


async def test_monitor_first_payment_owns_nonce_even_in_one_batch():
    """Dust copying a (now public) memo after the real payment must not
    displace it — rows arrive newest-first, the index must be oldest-wins."""
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    real = rpc.pay(source="buyer", dest="S", lamports=10**8, memo=pay_memo(PUB_NONCE))
    rpc.pay(source="attacker", dest="S", lamports=1, memo=pay_memo(PUB_NONCE))
    await mon.poll()
    assert mon.get(PUB_NONCE) == real


async def test_monitor_uses_watermark_and_paginates():
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    mon.PAGE_LIMIT = 2
    sigs = [rpc.pay(source="B", dest="S", lamports=1, memo=pay_memo(f"0000000{i}:{SID}"))
            for i in range(5)]
    await mon.poll()
    assert all(mon.get(f"0000000{i}:{SID}") == sigs[i] for i in range(5))
    assert [c["before"] for c in rpc.signature_calls] == [None, sigs[3], sigs[1]]

    rpc.signature_calls.clear()
    new = rpc.pay(source="B", dest="S", lamports=1, memo=pay_memo(f"00000009:{SID}"))
    await mon.poll()
    assert rpc.signature_calls[0]["until"] == sigs[4]
    assert mon.get(f"00000009:{SID}") == new


async def test_monitor_stays_unhealthy_when_rpc_fails():
    rpc = FakeRpc()
    rpc.fail_signatures = True
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    with pytest.raises(SolanaRpcError):
        await mon.poll()
    assert mon.is_healthy() is False


async def test_monitor_ignores_rows_older_than_cache_ttl():
    rpc = FakeRpc()
    mon = SolanaPaymentMonitor(rpc, "S")  # type: ignore[arg-type]
    rpc.pay(source="B", dest="S", lamports=1, memo=pay_memo(PUB_NONCE),
            block_time=int(time.time()) - mon.CACHE_TTL - 10)
    await mon.poll()
    assert mon.get(PUB_NONCE) is None


# ── verify ──────────────────────────────────────────────────────────────

def test_sol_rail_satisfies_chain_rail_protocol():
    assert isinstance(_rail(), ChainRail)


async def test_verify_by_nonce_returns_onchain_signature_not_client_proof():
    rpc = FakeRpc()
    rail = _rail(rpc)
    seller = str(rail.pay_to)
    real = rpc.pay(source="buyer", dest=seller, lamports=2_000_000, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()
    v = await rail.verify(proof="client-garbage", nonce=PUB_NONCE, min_amount=2_000_000)
    assert v.tx_hash == real
    assert (v.sender, v.amount, v.recipient, v.comment) == ("buyer", 2_000_000, seller, PUB_NONCE)


async def test_verify_uses_proof_hint_when_poller_lags():
    rpc = FakeRpc()
    rail = _rail(rpc)
    sig = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=10,
                  memo=pay_memo(PUB_NONCE), indexed=False)
    v = await rail.verify(proof=sig, nonce=PUB_NONCE, min_amount=10)
    assert v.tx_hash == sig
    assert rail.monitor.get(PUB_NONCE) == sig  # remembered for refund recovery


async def test_verify_rejects_hint_with_foreign_memo():
    """A real tx to us under another nonce must not satisfy this claim."""
    rpc = FakeRpc()
    rail = _rail(rpc)
    other = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=10**9,
                    memo=pay_memo(f"99999999:{SID}"), indexed=False)
    with pytest.raises(PaymentVerificationError, match="not found"):
        await rail.verify(proof=other, nonce=PUB_NONCE, min_amount=10)
    assert rail.monitor.get(PUB_NONCE) is None


async def test_verify_ignores_failed_hint_tx():
    rpc = FakeRpc()
    rail = _rail(rpc)
    sig = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=10,
                  memo=pay_memo(PUB_NONCE), err={"x": 1}, indexed=False)
    with pytest.raises(PaymentVerificationError, match="not found"):
        await rail.verify(proof=sig, nonce=PUB_NONCE, min_amount=10)


async def test_verify_underpaid_is_definitive_rejection():
    rpc = FakeRpc()
    rail = _rail(rpc)
    rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=9, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()
    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await rail.verify(proof="", nonce=PUB_NONCE, min_amount=10)


async def test_verify_underpaid_carries_payment_for_refund():
    rpc = FakeRpc()
    rail = _rail(rpc)
    sig = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=9, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()
    with pytest.raises(UnderpaidPayment) as ei:
        await rail.verify(proof="", nonce=PUB_NONCE, min_amount=10)
    assert (ei.value.payment.tx_hash, ei.value.payment.sender, ei.value.payment.amount) == (sig, "buyer", 9)
    assert ei.value.required == 10


async def test_verify_zero_amount_is_not_refundable():
    rpc = FakeRpc()
    rail = _rail(rpc)
    rpc.pay(source="buyer", dest=str(Keypair().pubkey()), lamports=10, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()
    with pytest.raises(PaymentVerificationError) as ei:
        await rail.verify(proof="", nonce=PUB_NONCE, min_amount=10)
    assert not isinstance(ei.value, UnderpaidPayment)


async def test_verify_accepts_topup_hint_only_after_underpaid_first_tx():
    rpc = FakeRpc()
    rail = _rail(rpc)
    first = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=10, memo=pay_memo(PUB_NONCE))
    topup = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=100, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()
    assert rail.monitor.get(PUB_NONCE) == first
    v = await rail.verify(proof=topup, nonce=PUB_NONCE, min_amount=100)
    assert v.tx_hash == topup
    # A full first payment is never displaced by a hint (hijack guard).
    v = await rail.verify(proof=topup, nonce=PUB_NONCE, min_amount=10)
    assert v.tx_hash == first


async def test_verify_expired_payment_rejected():
    rpc = FakeRpc()
    rail = _rail(rpc, payment_timeout_seconds=60)
    sig = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=10,
                  memo=pay_memo(PUB_NONCE), block_time=int(time.time()) - 120, indexed=False)
    with pytest.raises(PaymentVerificationError, match="expired"):
        await rail.verify(proof=sig, nonce=PUB_NONCE, min_amount=10)


async def test_verify_memo_without_transfer_to_us_rejected():
    rpc = FakeRpc()
    rail = _rail(rpc)
    rpc.pay(source="buyer", dest=str(Keypair().pubkey()), lamports=10**9, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()
    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await rail.verify(proof="", nonce=PUB_NONCE, min_amount=1)


async def test_verify_rpc_error_is_not_a_definitive_rejection():
    """Transport errors must surface as non-PaymentVerificationError so the
    handler queues a refund instead of 402-ing a possibly real payment."""
    rpc = FakeRpc()
    rail = _rail(rpc)
    rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=10, memo=pay_memo(PUB_NONCE))
    await rail.monitor.poll()

    async def boom(*a, **k):
        raise SolanaRpcError("rpc down")

    rpc.get_transaction = boom  # type: ignore[method-assign]
    with pytest.raises(SolanaRpcError):
        await rail.verify(proof="", nonce=PUB_NONCE, min_amount=10)


# ── 402 option ──────────────────────────────────────────────────────────

def test_payment_option_shape():
    rail = _rail()
    opt = rail.payment_option(1_000_000, PUB_NONCE)
    assert opt == {
        "rail": "SOL",
        "address": str(rail.pay_to),
        "amount": "1000000",
        "memo": f"CTLX:PAY:{PUB_NONCE}",
        "network": "devnet",
    }


# ── refunds ─────────────────────────────────────────────────────────────

async def test_refund_sends_transfer_minus_fee_with_refund_memo():
    rpc = FakeRpc()
    kp = Keypair()
    rail = _rail(rpc, kp=kp, refund_fee_lamports=5_000)
    buyer = Keypair().pubkey()
    rpc.accounts[str(buyer)] = {"lamports": 1}
    sig = await rail.refund(str(buyer), 2_000_000, original_tx_hash="solana:pub:abcd1234",
                            reason="agent_failed")
    assert sig is not None
    tx = _decode_sent(rpc.sent[0])
    msg = tx.message
    keys = list(msg.account_keys)
    assert keys[0] == kp.pubkey()
    assert tx.verify_with_results() == [True]
    by_program = {str(keys[ix.program_id_index]): ix for ix in msg.instructions}
    transfer_ix = by_program["11111111111111111111111111111111"]
    assert keys[transfer_ix.accounts[1]] == buyer
    assert int.from_bytes(bytes(transfer_ix.data)[4:12], "little") == 1_995_000
    memo = bytes(by_program[str(MEMO_PROGRAM)].data).decode()
    assert memo == refund_memo("solana:pub:abcd1234", "agent_failed", SID)
    assert json.loads(memo.removeprefix("CTLX:REFUND:"))["tx"] == "solana:pub:abcd1234"


async def test_refund_skipped_when_amount_not_above_fee():
    rpc = FakeRpc()
    rail = _rail(rpc, refund_fee_lamports=5_000)
    assert await rail.refund(str(Keypair().pubkey()), 5_000, original_tx_hash="t", reason="r") is None
    assert rpc.sent == []


async def test_refund_skipped_below_rent_for_new_account():
    rpc = FakeRpc()
    rail = _rail(rpc, refund_fee_lamports=0)
    to = str(Keypair().pubkey())
    assert await rail.refund(to, LIVE_RENT_0 - 1, original_tx_hash="t", reason="r") is None
    assert rpc.sent == []
    # Between the live rent and the stale 890_880 constant: must still refund.
    assert await rail.refund(to, RENT_EXEMPT_MIN_LAMPORTS - 1, original_tx_hash="t", reason="r") is not None
    rpc.sent.clear()
    # Existing account can receive any amount.
    rpc.accounts[to] = {"lamports": 1}
    assert await rail.refund(to, 1_000, original_tx_hash="t", reason="r") is not None


async def test_refund_send_failure_returns_none():
    rpc = FakeRpc()
    rpc.fail_send = True
    rail = _rail(rpc)
    assert await rail.refund(str(Keypair().pubkey()), 10**9, original_tx_hash="t", reason="r") is None


async def test_check_refund_balance_keeps_seller_rent_exempt():
    rpc = FakeRpc()
    rail = _rail(rpc, refund_fee_lamports=5_000)
    rpc.balance = 1_000_000 - 5_000 + 5_000 + LIVE_RENT_0
    assert (await rail.check_refund_balance(1_000_000))[0] is True
    rpc.balance -= 1
    ok, err = await rail.check_refund_balance(1_000_000)
    assert ok is False and "SOL balance" in err


async def test_find_existing_refund_matches_memo_for_original():
    rpc = FakeRpc()
    rail = _rail(rpc)
    seller = str(rail.pay_to)
    rpc.pay(source=seller, dest="X", lamports=1, memo=refund_memo("solana:pub:aaaaaaaa", "r", SID))
    hit = rpc.pay(source=seller, dest="Y", lamports=1, memo=refund_memo("solana:pub:bbbbbbbb", "r", SID))
    assert await rail.find_existing_refund("solana:pub:bbbbbbbb") == hit
    assert await rail.find_existing_refund("solana:pub:cccccccc") is None


async def test_find_existing_refund_pages_back_to_since():
    from chains.solana import refunds

    rpc = FakeRpc()
    rail = _rail(rpc)
    seller = str(rail.pay_to)
    now = int(time.time())
    old = rpc.pay(source=seller, dest="Y", lamports=1, block_time=now - 100,
                  memo=refund_memo("solana:pub:bbbbbbbb", "r", SID))
    for _ in range(refunds.DEDUP_PAGE_SIZE + 5):  # bury it past one page
        rpc.pay(source="buyer", dest=seller, lamports=1, memo=None, block_time=now)
    rpc.signature_calls.clear()
    assert await rail.find_existing_refund("solana:pub:bbbbbbbb") is None  # newest page only
    assert await rail.find_existing_refund("solana:pub:bbbbbbbb", since=now - 200) == old
    # Stops once a page reaches rows older than since (minus clock slack).
    rpc.signature_calls.clear()
    since = now + refunds.DEDUP_CLOCK_SLACK_SECONDS + 1
    assert await rail.find_existing_refund("solana:pub:cccccccc", since=since) is None
    assert len(rpc.signature_calls) == 1


async def test_find_existing_refund_inconclusive_past_page_cap(monkeypatch):
    from chains.solana import refunds

    monkeypatch.setattr(refunds, "DEDUP_PAGE_SIZE", 2)
    monkeypatch.setattr(refunds, "DEDUP_MAX_PAGES", 2)
    rpc = FakeRpc()
    rail = _rail(rpc)
    for _ in range(6):
        rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=1, memo=None)
    with pytest.raises(refunds.RefundDedupInconclusive):
        await rail.find_existing_refund("solana:pub:dddddddd", since=time.time() - 3600)


async def test_recover_payment_from_monitor():
    rpc = FakeRpc()
    rail = _rail(rpc)
    rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=77, memo=pay_memo(PUB_NONCE))
    assert await rail.recover_payment(PUB_NONCE) == ("buyer", 77)
    assert await rail.recover_payment(f"ffffffff:{SID}") is None


async def test_min_rent_uses_rpc_caches_and_falls_back():
    from chains.solana.rpc import SolanaRpc
    rpc = SolanaRpc("http://unused")
    calls = []

    async def fake_call(method, params=None):
        calls.append((method, params))
        return 650_240

    rpc.call = fake_call  # type: ignore[method-assign]
    assert await rpc.min_rent(0, fallback=1) == 650_240
    assert await rpc.min_rent(0, fallback=1) == 650_240
    assert calls == [("getMinimumBalanceForRentExemption", [0])]

    async def down(method, params=None):
        raise SolanaRpcError("down")

    rpc.call = down  # type: ignore[method-assign]
    assert await rpc.min_rent(165, fallback=2_039_280) == 2_039_280


async def test_close_stops_monitor_and_rpc():
    rpc = FakeRpc()
    rail = _rail(rpc)
    await rail.start()
    assert rail.monitor_healthy()
    await rail.close()
    assert rpc.closed


# ── settings ────────────────────────────────────────────────────────────

def _base_env(monkeypatch):
    monkeypatch.setenv("AGENT_COMMAND", "true")
    monkeypatch.setenv("AGENT_CAPABILITY", "x")
    monkeypatch.setenv("AGENT_NAME", "A")
    monkeypatch.setenv("AGENT_DESCRIPTION", "d")
    monkeypatch.setenv("AGENT_ENDPOINT", "https://a.test")
    monkeypatch.setenv("TON_WALLET_PK", "a" * 64)


def test_settings_sol_sku_token(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    kp = Keypair()
    monkeypatch.setenv("SOL_WALLET_PK", json.dumps(list(bytes(kp))))
    monkeypatch.setenv("SOL_CLUSTER", "devnet")
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:ton=1000000000:sol=2000000")
    s = load_settings(env_file="/nonexistent")
    assert s.payment_rails == ("TON", "SOL")
    assert s.skus[0].price_for("SOL") == 2_000_000
    assert s.skus[0].prices == {"TON": 1_000_000_000, "SOL": 2_000_000}
    assert s.agent_price_sol == 2_000_000
    assert s.sol_wallet == str(kp.pubkey())
    assert (s.sol_commitment, s.sol_refund_fee_lamports) == ("confirmed", 5_000)


def test_settings_sol_requires_wallet(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:ton=1:sol=2")
    with pytest.raises(RuntimeError, match="SOL rail requires SOL_WALLET_PK"):
        load_settings(env_file="/nonexistent")


def test_settings_sol_dynamic_price_rejected(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    monkeypatch.setenv("SOL_WALLET_PK", json.dumps(list(bytes(Keypair()))))
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:sol=0")
    with pytest.raises(RuntimeError, match="sol dynamic pricing"):
        load_settings(env_file="/nonexistent")


def test_settings_sol_commitment_validated(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    monkeypatch.setenv("SOL_WALLET_PK", json.dumps(list(bytes(Keypair()))))
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:sol=10")
    monkeypatch.setenv("SOL_COMMITMENT", "processed")
    with pytest.raises(RuntimeError, match="SOL_COMMITMENT"):
        load_settings(env_file="/nonexistent")
    monkeypatch.setenv("SOL_COMMITMENT", "finalized")
    assert load_settings(env_file="/nonexistent").sol_commitment == "finalized"


# ── /invoke end-to-end (fake RPC) ───────────────────────────────────────

def _sol_app(app_factory, rpc: FakeRpc, **overrides: Any):
    kp = Keypair()
    sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID,
        price_ton=1_000_000, price_usd=None, initial_stock=None, price_sol=2_000_000,
    )
    app = app_factory(
        skus=(sku,), payment_rails=("TON", "SOL"),
        sol_wallet_pk=json.dumps(list(bytes(kp))), sol_cluster="devnet",
        **overrides,
    )
    assert isinstance(app.rails["SOL"], SolRail)
    app.rails["SOL"] = _rail(rpc, kp=kp)
    return app


async def _preflight_sol_option(c) -> dict[str, Any]:
    resp = await c.post("/invoke", json={
        "capability": "translate", "rail": "SOL", "body": {"text": "hi"},
    })
    assert resp.status == 402, await resp.text()
    data = await resp.json()
    return next(o for o in data["payment_options"] if o["rail"] == "SOL")


async def test_invoke_sol_full_flow(app_factory, monkeypatch):
    import api as api_module

    async def fake_run(**kwargs):
        return {"result": {"type": "text", "data": "ok"}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)
    rail = app.rails["SOL"]
    await rail.monitor.poll()  # health gate needs one fresh poll
    async with _serve_app(app) as c:
        opt = await _preflight_sol_option(c)
        assert opt["address"] == str(rail.pay_to)
        assert opt["amount"] == "2000000"
        pub_nonce = extract_pay_nonce(opt["memo"])
        assert pub_nonce and pub_nonce.endswith(":sid-test")
        sec = opt["nonce"].split(":")[0][8:]
        assert sec not in opt["memo"]  # sec never on-chain

        sig = rpc.pay(source="buyer", dest=str(rail.pay_to), lamports=2_000_000, memo=opt["memo"])
        resp = await c.post("/invoke", json={
            "capability": "translate", "rail": "SOL", "proof": sig,
            "nonce": opt["nonce"], "body": {"text": "hi"},
        })
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["status"] == "done"
        pub = pub_nonce.split(":")[0]
        assert await app.tx_store.is_processed(f"solana:{sig}")
        assert await app.tx_store.is_processed(f"solana:pub:{pub}")
        assert rail.monitor.get(pub_nonce) is None  # consumed after durable write

        replay = await c.post("/invoke", json={
            "capability": "translate", "rail": "SOL", "proof": sig,
            "nonce": opt["nonce"], "body": {"text": "hi"},
        })
        assert replay.status != 200


async def test_invoke_sol_wrong_secret_never_touches_rpc(app_factory):
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)
    await app.rails["SOL"].monitor.poll()
    async with _serve_app(app) as c:
        opt = await _preflight_sol_option(c)
        sig = rpc.pay(source="buyer", dest=str(app.rails["SOL"].pay_to),
                      lamports=2_000_000, memo=opt["memo"])
        pub = opt["nonce"][:8]
        forged = f"{pub}deadbeef:sid-test"
        rpc.tx_calls.clear()
        resp = await c.post("/invoke", json={
            "capability": "translate", "rail": "SOL", "proof": sig,
            "nonce": forged, "body": {"text": "hi"},
        })
        assert resp.status == 403
        assert rpc.tx_calls == []
        assert not await app.tx_store.is_processed(f"solana:{sig}")


async def test_invoke_sol_underpaid_queues_refund_then_topup_delivers(app_factory, monkeypatch):
    """Underpaid: 402 + refund queued under the on-chain sig (sender/amount
    known). The pub stays claimable, so a top-up under the same memo delivers."""
    import api as api_module
    from api.domain.refund_worker import _process_entry

    async def fake_run(**kwargs):
        return {"result": {"type": "text", "data": "ok"}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc, rate_limit_requests=100)
    rail = app.rails["SOL"]
    await rail.monitor.poll()
    buyer = Keypair().pubkey()
    rpc.accounts[str(buyer)] = {"lamports": 1}
    async with _serve_app(app) as c:
        opt = await _preflight_sol_option(c)
        claim = {"capability": "translate", "rail": "SOL",
                 "nonce": opt["nonce"], "body": {"text": "hi"}}
        under = rpc.pay(source=str(buyer), dest=str(rail.pay_to),
                        lamports=1_500_000, memo=opt["memo"])
        resp = await c.post("/invoke", json={**claim, "proof": under})
        assert resp.status == 402
        body = await resp.json()
        assert body["refund_pending"] is True
        assert (body["tx"], body["paid"], body["required"]) == (under, "1500000", "2000000")
        entry = await app.refund_queue.get(f"solana:{under}")
        assert (entry.sender, entry.amount, entry.rail) == (str(buyer), 1_500_000, "SOL")

        # Retrying the same underpaid tx is idempotent (one queue row).
        resp = await c.post("/invoke", json={**claim, "proof": under})
        assert resp.status == 402

        # Worker refunds it on Solana.
        await _process_entry(app, entry)
        assert (await app.refund_queue.get(f"solana:{under}")).status == "refunded"
        assert len(rpc.sent) == 1

        topup = rpc.pay(source=str(buyer), dest=str(rail.pay_to),
                        lamports=2_000_000, memo=opt["memo"])
        resp = await c.post("/invoke", json={**claim, "proof": topup})
        assert resp.status == 200, await resp.text()
        assert await app.tx_store.is_processed(f"solana:{topup}")


async def test_invoke_sol_memo_without_money_is_plain_402(app_factory):
    """Memo present but nothing paid to us: nothing to refund."""
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)
    await app.rails["SOL"].monitor.poll()
    async with _serve_app(app) as c:
        opt = await _preflight_sol_option(c)
        sig = rpc.pay(source="buyer", dest=str(Keypair().pubkey()),
                      lamports=2_000_000, memo=opt["memo"])
        resp = await c.post("/invoke", json={
            "capability": "translate", "rail": "SOL", "proof": sig,
            "nonce": opt["nonce"], "body": {"text": "hi"},
        })
        assert resp.status == 402
        assert "refund_pending" not in await resp.json()
        assert await app.refund_queue.get(f"solana:{sig}") is None


async def test_preflight_503_when_sol_monitor_stale(app_factory):
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)  # no poll yet → unhealthy
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate", "rail": "SOL", "body": {"text": "hi"},
        })
        assert resp.status == 503


async def test_preflight_drops_stale_rail_and_lists_it_unavailable(app_factory):
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)  # SOL never polled → unhealthy, TON healthy
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={"capability": "translate", "body": {"text": "hi"}})
        assert resp.status == 402, await resp.text()
        data = await resp.json()
        assert [o["rail"] for o in data["payment_options"]] == ["TON"]
        assert data["unavailable_rails"] == [
            {"rail": "SOL", "reason": "monitor_degraded", "retry_after_seconds": 60},
        ]
        assert resp.headers["x-ton-pay-address"]
        info = await (await c.get("/info")).json()
        assert info["payment_rails"] == ["TON", "SOL"]
        assert info["unavailable_rails"] == ["SOL"]
        # Once SOL polls, it is offered again and nothing is unavailable.
        await app.rails["SOL"].monitor.poll()
        data = await (await c.post("/invoke", json={"capability": "translate", "body": {"text": "hi"}})).json()
        assert [o["rail"] for o in data["payment_options"]] == ["TON", "SOL"]
        assert "unavailable_rails" not in data
        assert "unavailable_rails" not in await (await c.get("/info")).json()


async def test_preflight_503_lists_rails_when_none_is_healthy(app_factory):
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)
    app.rails["TON"].monitor_healthy = lambda *_a, **_k: False
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={"capability": "translate", "body": {"text": "hi"}})
        assert resp.status == 503
        assert resp.headers["Retry-After"] == "60"
        data = await resp.json()
        assert [u["rail"] for u in data["unavailable_rails"]] == ["TON", "SOL"]


async def test_preflight_ton_down_still_sells_on_sol(app_factory):
    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)
    app.rails["TON"].monitor_healthy = lambda *_a, **_k: False
    await app.rails["SOL"].monitor.poll()
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={"capability": "translate", "body": {"text": "hi"}})
        assert resp.status == 402, await resp.text()
        data = await resp.json()
        assert [o["rail"] for o in data["payment_options"]] == ["SOL"]
        assert data["payment_request"]["rail"] == "SOL"
        assert [u["rail"] for u in data["unavailable_rails"]] == ["TON"]
        assert "x-ton-pay-address" not in resp.headers


async def test_refund_worker_recovers_and_refunds_sol(app_factory):
    """Verifier crashed mid-claim: worker recovers sender/amount from the SOL
    monitor and refunds on Solana, never touching the TON liteserver path."""
    from api.domain.refund_worker import _process_entry

    rpc = FakeRpc()
    app = _sol_app(app_factory, rpc)
    rail = app.rails["SOL"]
    buyer = Keypair().pubkey()
    rpc.accounts[str(buyer)] = {"lamports": 1}
    rpc.pay(source=str(buyer), dest=str(rail.pay_to), lamports=2_000_000, memo=pay_memo(PUB_NONCE))
    key = "solana:pub:abcd1234"
    try:
        await app.refund_queue.enqueue(tx_hash=key, nonce=PUB_NONCE, rail="SOL")
        entry = await app.refund_queue.get(key)
        await _process_entry(app, entry)
        done = await app.refund_queue.get(key)
        assert done.status == "refunded", done
        assert done.sender == str(buyer) and done.amount == 2_000_000
        assert len(rpc.sent) == 1
    finally:
        await app.refund_queue.close()
        await app.tx_store.close()


# ── USDC rail: refund-worker hooks ──────────────────────────────────────

def _usdc_rail(rpc: FakeRpc, kp: Keypair | None = None):
    from chains.solana.rail_usdc import UsdcRail
    return UsdcRail(keypair=kp or Keypair(), rpc=rpc, cluster="devnet",  # type: ignore[arg-type]
                    get_sidecar_id=lambda: SID)


def _with_token_balance(rpc: FakeRpc, amount: int | None) -> None:
    async def bal(_acct):
        return amount
    rpc.get_token_account_balance = bal  # type: ignore[attr-defined]


async def test_usdc_check_refund_balance():
    rpc = FakeRpc()
    rail = _usdc_rail(rpc)
    _with_token_balance(rpc, 1_000_000)
    rpc.balance = 5_000 + LIVE_RENT_165 + LIVE_RENT_0
    assert await rail.check_refund_balance(1_000_000) == (True, "")
    ok, err = await rail.check_refund_balance(1_000_001)
    assert not ok and "USDC balance" in err
    _with_token_balance(rpc, None)  # seller ATA missing
    assert (await rail.check_refund_balance(1))[0] is False
    _with_token_balance(rpc, 10**9)
    rpc.balance -= 1
    ok, err = await rail.check_refund_balance(1)
    assert not ok and "SOL balance" in err


async def test_usdc_refund_carries_refund_memo_and_is_found_for_dedup():
    rpc = FakeRpc()
    kp = Keypair()
    rail = _usdc_rail(rpc, kp)
    buyer = str(Keypair().pubkey())
    sig = await rail.refund(buyer, 1_000_000, original_tx_hash="solana:abc", reason="agent_failed")
    assert sig is not None
    msg = _decode_sent(rpc.sent[0]).message
    keys = list(msg.account_keys)
    memos = [bytes(ix.data).decode() for ix in msg.instructions
             if keys[ix.program_id_index] == MEMO_PROGRAM]
    assert memos == [refund_memo("solana:abc", "agent_failed", SID)]
    rpc.rows.insert(0, {"signature": sig, "err": None, "memo": f"[80] {memos[0]}"})
    assert await rail.find_existing_refund("solana:abc") == sig
    assert await rail.find_existing_refund("solana:other") is None


def _transfer_checked_amount(msg) -> int:
    import struct as _struct
    keys = list(msg.account_keys)
    for ix in msg.instructions:
        data = bytes(ix.data)
        if str(keys[ix.program_id_index]) == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" and data[0] == 12:
            return _struct.unpack_from("<Q", data, 1)[0]
    raise AssertionError("no TransferChecked")


async def test_usdc_refund_keeps_fixed_fee():
    from solders.token.associated import get_associated_token_address
    from solders.pubkey import Pubkey
    from chains.solana.constants import TOKEN_PROGRAM, USDC_MINT_DEVNET

    rpc = FakeRpc()
    rail = _usdc_rail(rpc)
    buyer = Keypair().pubkey()
    ata = get_associated_token_address(buyer, Pubkey.from_string(USDC_MINT_DEVNET), TOKEN_PROGRAM)
    rpc.accounts[str(ata)] = {"lamports": 2_039_280}
    assert await rail.refund(str(buyer), 10_000, original_tx_hash="solana:a", reason="x") is not None
    assert _transfer_checked_amount(_decode_sent(rpc.sent[-1]).message) == 10_000 - 5_000
    # Fee eats the whole amount: no refund at all.
    n = len(rpc.sent)
    assert await rail.refund(str(buyer), 5_000, original_tx_hash="solana:b", reason="x") is None
    assert len(rpc.sent) == n


async def test_usdc_refund_charges_account_recreation():
    """Buyer closed their USDC account: the seller pays its rent, the buyer is
    charged the extra fee; below it the refund is skipped."""
    rpc = FakeRpc()
    rail = _usdc_rail(rpc)
    buyer = str(Keypair().pubkey())
    assert await rail.refund(buyer, 1_000_000, original_tx_hash="solana:a", reason="x") is not None
    assert _transfer_checked_amount(_decode_sent(rpc.sent[-1]).message) == 1_000_000 - 205_000
    n = len(rpc.sent)
    assert await rail.refund(buyer, 100_000, original_tx_hash="solana:b", reason="x") is None
    assert len(rpc.sent) == n


async def test_refund_worker_usdc_never_touches_ton(app_factory, monkeypatch):
    """Balance check + send go through the USDC rail, not the TON liteserver."""
    import api.domain.refund_worker as rw
    from tonutils.clients import LiteBalancer

    def no_ton(*a, **k):
        raise AssertionError("TON liteserver used for a USDC refund")

    monkeypatch.setattr(LiteBalancer, "from_network_config", no_ton)
    monkeypatch.setattr(rw, "_acquire_lite_client", no_ton)
    kp = Keypair()
    sku = AgentSku(sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID, price_ton=1_000_000,
                   price_usd=None, initial_stock=None, price_usdc=1_000_000)
    app = app_factory(skus=(sku,), payment_rails=("TON", "USDC"),
                      sol_wallet_pk=json.dumps(list(bytes(kp))), sol_cluster="devnet")
    rpc = FakeRpc()
    _with_token_balance(rpc, 10**9)
    app.rails["USDC"] = _usdc_rail(rpc, kp)
    key = "solana:usdc-identity"
    try:
        await app.refund_queue.enqueue(tx_hash=key, nonce="sig", rail="USDC",
                                       sender=str(Keypair().pubkey()), amount=1_000_000)
        await rw._process_entry(app, await app.refund_queue.get(key))
        assert (await app.refund_queue.get(key)).status == "refunded"
        assert len(rpc.sent) == 1
    finally:
        await app.refund_queue.close()
        await app.tx_store.close()


# ── quotes are TON/USDT-only ────────────────────────────────────────────

def _quoted_sol_app(app_factory, rpc: FakeRpc):
    from api import QuoteEntry
    app = _sol_app(app_factory, rpc, has_quote=True, rate_limit_requests=100)
    app.quotes["q1"] = QuoteEntry(price=5_000_000, expires_at=time.time() + 300, sku_id=DEFAULT_SKU_ID)
    return app


async def test_quote_claim_on_sol_rejected_without_locking_quote(app_factory):
    rpc = FakeRpc()
    app = _quoted_sol_app(app_factory, rpc)
    await app.rails["SOL"].monitor.poll()
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate", "rail": "SOL", "quote_id": "q1",
            "proof": _sig(), "nonce": f"abcd1234deadbeef:{SID}", "body": {"text": "hi"},
        })
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "quote_not_supported_for_rail"
        assert body["supported_rails"] == ["TON", "USDT"]
        assert app.quotes["q1"].locked is False
        assert rpc.tx_calls == []

        # USDC (x402) is refused the same way.
        resp = await c.post("/invoke", json={
            "capability": "translate", "rail": "USDC", "quote_id": "q1", "body": {"text": "hi"},
        })
        assert resp.status == 400


async def test_quoted_preflight_offers_only_quote_rails(app_factory):
    rpc = FakeRpc()
    app = _quoted_sol_app(app_factory, rpc)
    await app.rails["SOL"].monitor.poll()
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate", "quote_id": "q1", "body": {"text": "hi"},
        })
        assert resp.status == 402
        rails = [o["rail"] for o in (await resp.json())["payment_options"]]
        assert rails == ["TON"]
        # Without a quote the SOL option is still there.
        resp = await c.post("/invoke", json={"capability": "translate", "body": {"text": "hi"}})
        assert [o["rail"] for o in (await resp.json())["payment_options"]] == ["TON", "SOL"]
