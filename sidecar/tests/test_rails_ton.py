"""Tests for the TON ChainRail adapters (chains.ton.rail_ton / rail_usdt).

Step 3 of the multichain refactor. These prove the adapters satisfy the
ChainRail protocol and that their behaviour matches the current TON paths
bit-for-bit, so step 4 can swap the handlers' literal branching for rail
objects without changing the wire contract:
- ``refund`` mirrors the per-rail branch of ``api.domain.refund.refund_user``
  (cross-checked against tests/test_refund.py).
- ``payment_option`` mirrors the per-rail dict in ``build_402_response``
  (cross-checked against the USDT 402 tests in tests/test_api.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chains.base import ChainRail
from chains.ton.jetton import USDT_MASTER_TESTNET, USDT_REFUND_FEE
from chains.ton.rail_ton import TonRail
from chains.ton.rail_usdt import UsdtRail
from payments.types import VerifiedPayment


def _ton_rail(verifier=None, sender=None, refund_fee_nanoton=500_000):
    return TonRail(
        get_verifier=lambda: verifier,
        sender=sender or SimpleNamespace(send=AsyncMock()),
        agent_wallet="EQagent",
        sidecar_id="sid-test",
        refund_fee_nanoton=refund_fee_nanoton,
    )


def _usdt_rail(verifier=None, sender=None, jetton_wallet="EQjw"):
    return UsdtRail(
        get_verifier=lambda: verifier,
        get_agent_jetton_wallet=lambda: jetton_wallet,
        sender=sender or SimpleNamespace(send_jetton=AsyncMock()),
        agent_wallet="EQagent",
        usdt_master=USDT_MASTER_TESTNET,
        sidecar_id="sid-test",
    )


# ── protocol conformance ───────────────────────────────────────────────


def test_rails_satisfy_chainrail_protocol():
    assert isinstance(_ton_rail(), ChainRail)
    assert isinstance(_usdt_rail(), ChainRail)
    assert _ton_rail().rail_id == "TON"
    assert _usdt_rail().rail_id == "USDT"


# ── verify delegates to the wrapped verifier ───────────────────────────


async def test_ton_verify_delegates_to_verifier():
    payment = VerifiedPayment(tx_hash="real", sender="EQs", recipient="EQagent",
                              amount=1000, comment="n")
    verifier = SimpleNamespace(verify=AsyncMock(return_value=payment))
    rail = _ton_rail(verifier=verifier)
    out = await rail.verify("user-tx", "n:sid-test", 900)
    assert out is payment
    verifier.verify.assert_awaited_once_with(
        tx_hash="user-tx", raw_nonce="n:sid-test", min_amount=900)


async def test_usdt_verify_delegates_to_verifier():
    payment = VerifiedPayment(tx_hash="real", sender="EQs", recipient="EQagent",
                              amount=2_000_000, comment="n")
    verifier = SimpleNamespace(verify=AsyncMock(return_value=payment))
    out = await _usdt_rail(verifier=verifier).verify("tx", "n", 2_000_000)
    assert out is payment


async def test_verify_raises_when_verifier_absent():
    with pytest.raises(RuntimeError, match="not started"):
        await _ton_rail(verifier=None).verify("tx", "n", 1)
    with pytest.raises(RuntimeError, match="not started"):
        await _usdt_rail(verifier=None).verify("tx", "n", 1)


# ── refund: bit-for-bit with refund_user per-rail branch ───────────────


async def test_ton_refund_sends_amount_minus_fee():
    sender = SimpleNamespace(send=AsyncMock(return_value="TON_REFUND"))
    rail = _ton_rail(sender=sender, refund_fee_nanoton=100)
    out = await rail.refund("EQu", 1000, original_tx_hash="tx", reason="timeout")
    assert out == "TON_REFUND"
    dest, amount, _body = sender.send.call_args.args
    assert dest == "EQu" and amount == 900


async def test_ton_refund_skipped_when_amount_below_fee():
    sender = SimpleNamespace(send=AsyncMock())
    out = await _ton_rail(sender=sender, refund_fee_nanoton=1000).refund(
        "EQu", 500, original_tx_hash="tx", reason="timeout")
    assert out is None
    sender.send.assert_not_awaited()


async def test_ton_refund_swallows_send_exception():
    sender = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("ton down")))
    out = await _ton_rail(sender=sender, refund_fee_nanoton=100).refund(
        "EQu", 1000, original_tx_hash="tx", reason="timeout")
    assert out is None


async def test_usdt_refund_sends_jetton_amount_minus_usdt_fee():
    sender = SimpleNamespace(send_jetton=AsyncMock(return_value="USDT_REFUND"))
    rail = _usdt_rail(sender=sender, jetton_wallet="EQjw")
    out = await rail.refund("EQu", 1_000_000, original_tx_hash="tx", reason="timeout")
    assert out == "USDT_REFUND"
    kwargs = sender.send_jetton.call_args.kwargs
    assert kwargs["own_jetton_wallet"] == "EQjw"
    assert kwargs["destination"] == "EQu"
    assert kwargs["jetton_amount"] == 1_000_000 - USDT_REFUND_FEE


async def test_usdt_refund_uses_empty_string_when_jetton_wallet_unknown():
    sender = SimpleNamespace(send_jetton=AsyncMock(return_value="R"))
    rail = _usdt_rail(sender=sender, jetton_wallet=None)
    await rail.refund("EQu", 1_000_000, original_tx_hash="tx", reason="r")
    assert sender.send_jetton.call_args.kwargs["own_jetton_wallet"] == ""


async def test_usdt_refund_skipped_when_amount_below_usdt_fee():
    sender = SimpleNamespace(send_jetton=AsyncMock())
    out = await _usdt_rail(sender=sender).refund(
        "EQu", USDT_REFUND_FEE, original_tx_hash="tx", reason="r")
    assert out is None
    sender.send_jetton.assert_not_awaited()


async def test_usdt_refund_swallows_send_exception():
    sender = SimpleNamespace(send_jetton=AsyncMock(side_effect=RuntimeError("down")))
    out = await _usdt_rail(sender=sender).refund(
        "EQu", 1_000_000, original_tx_hash="tx", reason="r")
    assert out is None


# ── payment_option: matches build_402_response wire shape ──────────────


def test_ton_payment_option_shape():
    opt = _ton_rail().payment_option(1_000_000, "nonce:sid-test")
    assert opt == {
        "rail": "TON",
        "address": "EQagent",
        "amount": "1000000",
        "memo": "nonce:sid-test",
    }


def test_usdt_payment_option_shape_includes_token_block():
    opt = _usdt_rail().payment_option(2_000_000, "nonce:sid-test")
    assert opt == {
        "rail": "USDT",
        "address": "EQagent",
        "amount": "2000000",
        "memo": "nonce:sid-test",
        "token": {"symbol": "USDT", "master": USDT_MASTER_TESTNET, "decimals": 6},
    }


# ── monitor_healthy ────────────────────────────────────────────────────


def test_monitor_healthy_false_when_verifier_absent():
    assert _ton_rail(verifier=None).monitor_healthy() is False
    assert _usdt_rail(verifier=None).monitor_healthy() is False


def test_monitor_healthy_delegates_to_verifier():
    healthy = SimpleNamespace(is_healthy=lambda max_age_seconds=60.0: True)
    unhealthy = SimpleNamespace(is_healthy=lambda max_age_seconds=60.0: False)
    assert _ton_rail(verifier=healthy).monitor_healthy() is True
    assert _ton_rail(verifier=unhealthy).monitor_healthy() is False
