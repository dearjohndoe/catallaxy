"""SVM exact inspect + settle (mocked RPC)."""

from __future__ import annotations

import base64
import json
import struct
from unittest.mock import AsyncMock

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from chains.base import ChainRail
from chains.solana.constants import CAIP2_DEVNET, TOKEN_PROGRAM, USDC_MINT_DEVNET
from chains.solana.exact import ExactSettler, inspect_exact, signing_payload
from chains.solana.rail_usdc import UsdcRail
from payments.types import PaymentVerificationError


def _partial_exact(
    *,
    fee: Keypair,
    buyer: Keypair,
    pay_to: Pubkey,
    mint: Pubkey,
    amount: int,
    dest: Pubkey | None = None,
    extra: list[Instruction] | None = None,
    token_program: Pubkey = TOKEN_PROGRAM,
    cu_price: int = 1,
    cu_limit: int = 20_000,
) -> VersionedTransaction:
    src = get_associated_token_address(buyer.pubkey(), mint, token_program)
    dest = dest or get_associated_token_address(pay_to, mint, token_program)
    data = bytes([12]) + struct.pack("<Q", amount) + bytes([6])
    transfer = Instruction(token_program, data, [
        AccountMeta(src, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(dest, False, True),
        AccountMeta(buyer.pubkey(), True, False),
    ])
    ixs = [set_compute_unit_limit(cu_limit), set_compute_unit_price(cu_price), transfer]
    if extra:
        ixs.extend(extra)
    msg = MessageV0.try_compile(fee.pubkey(), ixs, [], Hash.default())
    payload = (b"\x80" + bytes(msg)) if True else bytes(msg)
    n = msg.header.num_required_signatures
    keys = list(msg.account_keys)
    sigs = [Signature.default()] * n
    buyer_idx = keys.index(buyer.pubkey())
    sigs[buyer_idx] = buyer.sign_message(payload)
    return VersionedTransaction.populate(msg, sigs)


def _proof(tx: VersionedTransaction, network: str = CAIP2_DEVNET) -> str:
    inner = base64.b64encode(bytes(tx)).decode()
    return base64.b64encode(json.dumps({
        "x402Version": 2,
        "scheme": "exact",
        "network": network,
        "payload": {"transaction": inner},
    }).encode()).decode()


def test_inspect_happy_path():
    fee, buyer, seller = Keypair(), Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=10_000)
    # fee payer in the compiled tx is seller (first account), not `fee`
    out = inspect_exact(
        _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
        mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
    )
    assert out.sender == str(buyer.pubkey())
    assert out.amount == 10_000
    assert out.identity.startswith("solana:pub:")


def test_inspect_rejects_wrong_amount():
    seller, buyer = Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=9)
    with pytest.raises(PaymentVerificationError, match="amount mismatch"):
        inspect_exact(
            _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
            mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
        )


def test_inspect_rejects_wrong_dest():
    seller, buyer, other = Keypair(), Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    dest = get_associated_token_address(other.pubkey(), mint)
    tx = _partial_exact(
        fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint,
        amount=10_000, dest=dest,
    )
    with pytest.raises(PaymentVerificationError, match="destination"):
        inspect_exact(
            _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
            mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
        )


def test_inspect_rejects_fee_payer_mismatch():
    seller, buyer, stranger = Keypair(), Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=stranger, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=10_000)
    with pytest.raises(PaymentVerificationError, match="fee payer"):
        inspect_exact(
            _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
            mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
        )


def test_inspect_rejects_wrong_network():
    seller, buyer = Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=10_000)
    with pytest.raises(PaymentVerificationError, match="network"):
        inspect_exact(
            _proof(tx, network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"),
            min_amount=10_000, pay_to=seller.pubkey(),
            mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
        )


async def test_settle_cosigns_and_returns_signature():
    seller, buyer = Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=10_000)
    inspected = inspect_exact(
        _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
        mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
    )
    rpc = AsyncMock()
    rpc.send_transaction = AsyncMock(return_value="OnChainSig111111111111111111111111111111111")
    rpc.signature_status = AsyncMock(return_value={"confirmationStatus": "confirmed", "err": None})
    settler = ExactSettler(keypair=seller, rpc=rpc)
    paid = await settler.settle(inspected)
    assert paid.tx_hash.startswith("OnChainSig")
    assert paid.sender == str(buyer.pubkey())
    assert paid.amount == 10_000
    rpc.send_transaction.assert_awaited_once()
    sent_b64 = rpc.send_transaction.await_args.args[0]
    sent = VersionedTransaction.from_bytes(base64.b64decode(sent_b64))
    assert sent.signatures[0] != Signature.default()
    assert sent.signatures[0].verify(seller.pubkey(), signing_payload(sent))


async def test_settle_duplicate_payload_rejected():
    seller, buyer = Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=10_000)
    inspected = inspect_exact(
        _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
        mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
    )
    rpc = AsyncMock()
    rpc.send_transaction = AsyncMock(side_effect=RuntimeError("hang"))
    # First settle fails after cache insert... ExactSettler pops cache on error.
    settler = ExactSettler(keypair=seller, rpc=rpc)
    with pytest.raises(RuntimeError):
        await settler.settle(inspected)
    rpc.send_transaction = AsyncMock(return_value="sig")
    rpc.signature_status = AsyncMock(return_value={"confirmationStatus": "confirmed"})
    paid = await settler.settle(inspected)
    assert paid.tx_hash == "sig"
    with pytest.raises(PaymentVerificationError, match="duplicate_settlement"):
        await settler.settle(inspected)


def test_usdc_rail_is_chainrail_and_402_shape():
    seller = Keypair()
    rpc = AsyncMock()
    rail = UsdcRail(keypair=seller, rpc=rpc, cluster="devnet", mint=USDC_MINT_DEVNET)
    assert isinstance(rail, ChainRail)
    assert rail.rail_id == "USDC"
    opt = rail.payment_option(10_000, "deadbeef")
    assert opt["rail"] == "USDC"
    assert opt["extra"]["feePayer"] == str(seller.pubkey())
    assert opt["token"]["decimals"] == 6
    acc = rail.x402_accepts_entry(10_000, resource_url="https://a.test")
    assert acc["scheme"] == "exact"
    assert acc["network"] == CAIP2_DEVNET
    assert rail.monitor_healthy() is False  # not started: no health probe yet


async def test_usdc_rail_health_follows_rpc_probe():
    rpc = AsyncMock()
    rail = UsdcRail(keypair=Keypair(), rpc=rpc, cluster="devnet", mint=USDC_MINT_DEVNET)
    rpc.get_health = AsyncMock(return_value=False)
    await rail.start()
    assert rail.monitor_healthy() is False
    rpc.get_health = AsyncMock(return_value=True)
    assert await rail.probe() is True
    assert rail.monitor_healthy() is True
    # A later failed probe keeps the last success until it ages out.
    rpc.get_health = AsyncMock(return_value=False)
    await rail.probe()
    assert rail.monitor_healthy() is True
    rail._last_ok_at -= 61
    assert rail.monitor_healthy(60.0) is False
    await rail.close()
    assert rail._probe_task is None


def test_fee_caps_bound_what_the_seller_pays():
    """We pay the fee: CU price ≤ spec cap, CU limit ≤ 60k, priority ≤ 30k lamports.
    A real Solflare payment (limit 50k, price 100k µlamports = 5k lamports) passes."""
    buyer, seller = Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)

    def inspect(limit: int, price: int):
        tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint,
                            amount=10_000, cu_price=price, cu_limit=limit)
        return inspect_exact(_proof(tx), min_amount=10_000, pay_to=seller.pubkey(), mint=mint,
                             fee_payer=seller.pubkey(), cluster="devnet")

    assert inspect(50_000, 100_000).amount == 10_000          # Solflare on devnet
    assert inspect(6_000, 5_000_000).amount == 10_000         # spec price cap, 30k lamports
    assert inspect(60_000, 500_000).amount == 10_000          # limit cap, 30k lamports
    with pytest.raises(PaymentVerificationError, match="price exceeds cap"):
        inspect(1_000, 5_000_001)
    with pytest.raises(PaymentVerificationError, match="limit exceeds cap"):
        inspect(1_400_000, 1)                                  # griefing: big limit
    with pytest.raises(PaymentVerificationError, match="priority fee exceeds cap"):
        inspect(60_000, 600_000)                               # 36k lamports


async def test_settle_waits_past_processed(monkeypatch):
    """`processed` (seen by one RPC node, `confirmations: 0`) is not enough:
    settle returns only once the tx is confirmed."""
    import asyncio as _asyncio

    async def no_sleep(_):
        return None

    monkeypatch.setattr(_asyncio, "sleep", no_sleep)
    seller, buyer = Keypair(), Keypair()
    mint = Pubkey.from_string(USDC_MINT_DEVNET)
    tx = _partial_exact(fee=seller, buyer=buyer, pay_to=seller.pubkey(), mint=mint, amount=10_000)
    inspected = inspect_exact(
        _proof(tx), min_amount=10_000, pay_to=seller.pubkey(),
        mint=mint, fee_payer=seller.pubkey(), cluster="devnet",
    )
    processed = {"confirmationStatus": "processed", "confirmations": 0, "err": None}
    rpc = AsyncMock()
    rpc.send_transaction = AsyncMock(return_value="OnChainSig111111111111111111111111111111111")
    rpc.signature_status = AsyncMock(return_value=processed)
    with pytest.raises(PaymentVerificationError, match="not confirmed"):
        await ExactSettler(keypair=seller, rpc=rpc).settle(inspected)

    rpc.signature_status = AsyncMock(side_effect=[processed, processed, {"confirmationStatus": "confirmed", "err": None}])
    paid = await ExactSettler(keypair=seller, rpc=rpc).settle(inspected)
    assert paid.amount == 10_000
    assert rpc.signature_status.await_count == 3
