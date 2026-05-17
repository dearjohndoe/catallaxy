"""Tests for the USDT (jetton) inbound payment path.

Mirrors test_verify.py (which covers the TON rail) on the jetton rail:
JettonPaymentVerifier.verify() security gate + JettonWalletMonitor polling.
The network boundary (LiteBalancer.get_transactions) is mocked; all parsing,
nonce extraction and the verification logic run for real.

The headline jetton-specific check is
``test_jetton_monitor_poll_rejects_tx_from_wrong_jetton_wallet``: anyone can
send a message carrying a transfer_notification opcode from an arbitrary
address. Only notifications coming from *our* jetton wallet contract represent
a real USDT credit — accepting a forged one would let an attacker invoke a
paid agent for free.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytoniq_core import Cell, begin_cell

from jetton import TRANSFER_NOTIFICATION_OPCODE
from transfer import PAYMENT_OPCODE
from payments import (
    JettonPaymentTx,
    JettonPaymentVerifier,
    JettonWalletMonitor,
    PaymentVerificationError,
)

# A valid bounceable address used for the notification body's `sender` field
# (must round-trip through store_address/load_address). Wallet *identity*
# comparisons in the monitor use mocked `src.to_str`, so plain strings suffice
# there.
_ADDR = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
_JETTON_WALLET = "EQ_agent_jetton_wallet"


def _nonce_payload(nonce: str) -> Cell:
    return (
        begin_cell()
        .store_uint(PAYMENT_OPCODE, 32)
        .store_snake_string(nonce)
        .end_cell()
    )


def _notification_body(
    *, amount: int = 2_000_000, sender: str = _ADDR, nonce: str | None = "n:sid-test",
) -> Cell:
    """Build a TEP-74 transfer_notification cell with an optional nonce payload."""
    b = (
        begin_cell()
        .store_uint(TRANSFER_NOTIFICATION_OPCODE, 32)
        .store_uint(0, 64)            # query_id
        .store_coins(amount)
        .store_address(sender)
    )
    if nonce is None:
        b.store_bit(0)               # no forward_payload
    else:
        b.store_bit(1)
        b.store_ref(_nonce_payload(nonce))
    return b.end_cell()


def _chain_tx(*, lt: int, now: int, src_addr: str | None, body: Cell | None):
    """Fake on-chain tx as consumed by JettonWalletMonitor._poll."""
    if body is None:
        in_msg = None
    else:
        src = MagicMock()
        if src_addr is None:
            src.to_str = MagicMock(side_effect=RuntimeError("bad addr"))
        else:
            src.to_str = MagicMock(return_value=src_addr)
        in_msg = SimpleNamespace(info=SimpleNamespace(src=src), body=body)
    return SimpleNamespace(lt=lt, now=now, in_msg=in_msg)


def _jpx(*, amount: int, sender: str, now_ts: int, nonce: str = "n:sid-test",
         hash_hex: str = "cc" * 32) -> JettonPaymentTx:
    cell = MagicMock()
    cell.hash = bytes.fromhex(hash_hex)
    tx = SimpleNamespace(lt=1, now=now_ts, cell=cell)
    return JettonPaymentTx(tx=tx, amount=amount, sender=sender, nonce=nonce)


def _verifier(min_amount: int = 1_000, timeout: int = 300) -> JettonPaymentVerifier:
    return JettonPaymentVerifier(
        agent_wallet="EQagent", usdt_master="EQusdtmaster",
        min_amount=min_amount, payment_timeout_seconds=timeout, testnet=True,
    )


# ── JettonPaymentVerifier.verify ───────────────────────────────────────

async def test_jetton_verifier_raises_when_not_started():
    v = _verifier()
    with pytest.raises(RuntimeError, match="not started"):
        await v.verify("tx", "nonce")


async def test_jetton_verifier_success_returns_onchain_hash():
    v = _verifier(min_amount=1_000)
    entry = _jpx(amount=2_000_000, sender="EQpayer", now_ts=int(time.time()),
                 nonce="abc:sid-test", hash_hex="dd" * 32)
    monitor = MagicMock()
    monitor.get = MagicMock(return_value=entry)
    monitor.consume = MagicMock(return_value=entry)
    v._monitor = monitor

    result = await v.verify(tx_hash="user-supplied", raw_nonce="abc:sid-test")

    # Same security contract as the TON rail: the trusted hash is the real
    # on-chain cell hash, never the user-supplied string.
    assert result.tx_hash == "dd" * 32
    assert result.tx_hash != "user-supplied"
    assert result.sender == "EQpayer"
    assert result.amount == 2_000_000
    assert result.recipient == "EQagent"
    assert result.comment == "abc:sid-test"
    monitor.consume.assert_called_once_with("abc:sid-test")


async def test_jetton_verifier_amount_below_min_rejected():
    v = _verifier(min_amount=2_000_000)
    entry = _jpx(amount=1_999_999, sender="EQpayer", now_ts=int(time.time()))
    v._monitor = MagicMock()
    v._monitor.get = MagicMock(return_value=entry)
    v._monitor.consume = MagicMock()

    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await v.verify("tx", "n:sid-test")
    # Underpaid tx must NOT be consumed (so a later correct payment can match).
    v._monitor.consume.assert_not_called()


async def test_jetton_verifier_min_amount_override_rejects():
    v = _verifier(min_amount=1_000)
    entry = _jpx(amount=5_000, sender="EQpayer", now_ts=int(time.time()))
    v._monitor = MagicMock()
    v._monitor.get = MagicMock(return_value=entry)
    v._monitor.consume = MagicMock()

    # Per-SKU override raises the bar above what was paid.
    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await v.verify("tx", "n:sid-test", min_amount=10_000)


async def test_jetton_verifier_session_expired():
    v = _verifier(min_amount=1_000, timeout=60)
    stale = _jpx(amount=2_000_000, sender="EQpayer",
                 now_ts=int(time.time()) - 120)
    v._monitor = MagicMock()
    v._monitor.get = MagicMock(return_value=stale)
    v._monitor.consume = MagicMock()

    with pytest.raises(PaymentVerificationError, match="session expired"):
        await v.verify("tx", "n:sid-test")


async def test_jetton_verifier_missing_sender_rejected():
    v = _verifier(min_amount=1_000)
    entry = _jpx(amount=2_000_000, sender="", now_ts=int(time.time()))
    v._monitor = MagicMock()
    v._monitor.get = MagicMock(return_value=entry)
    v._monitor.consume = MagicMock()

    with pytest.raises(PaymentVerificationError, match="sender is missing"):
        await v.verify("tx", "n:sid-test")


async def test_jetton_verifier_timeout_when_tx_never_appears(monkeypatch):
    v = _verifier(min_amount=1_000)
    monitor = MagicMock()
    monitor.get = MagicMock(return_value=None)
    monitor.force = MagicMock()
    v._monitor = monitor

    monkeypatch.setattr(JettonPaymentVerifier, "VERIFY_TIMEOUT", 0.05)
    monkeypatch.setattr(JettonPaymentVerifier, "VERIFY_POLL", 0.01)

    with pytest.raises(PaymentVerificationError, match="not found"):
        await v.verify("tx", "n:sid-test")
    # While waiting it must have nudged the monitor to poll.
    assert monitor.force.called


# ── JettonWalletMonitor ────────────────────────────────────────────────

def _monitor(client, poll_interval: int = 10) -> JettonWalletMonitor:
    return JettonWalletMonitor(
        client=client, agent_address="EQagent",
        jetton_wallet_address=_JETTON_WALLET, poll_interval=poll_interval,
    )


async def test_jetton_monitor_get_and_consume_trim_whitespace():
    m = _monitor(MagicMock())
    entry = _jpx(amount=1, sender="EQp", now_ts=int(time.time()))
    m._by_nonce["nonce-1"] = entry

    assert m.get("  nonce-1  ") is entry
    assert m.consume("nonce-1 ") is entry
    assert m.get("nonce-1") is None


async def test_jetton_monitor_poll_caches_valid_notification():
    client = MagicMock()
    now_ts = int(time.time())
    tx = _chain_tx(lt=100, now=now_ts, src_addr=_JETTON_WALLET,
                   body=_notification_body(amount=3_000_000, nonce="good:sid"))
    client.get_transactions = AsyncMock(side_effect=[[tx], []])

    m = _monitor(client)
    await m._poll()

    cached = m.get("good:sid")
    assert cached is not None
    assert cached.amount == 3_000_000
    assert cached.nonce == "good:sid"
    # Watermark advances so the next poll won't reprocess this tx.
    assert m._last_processed_lt == 100


async def test_jetton_monitor_poll_rejects_tx_from_wrong_jetton_wallet():
    """SECURITY: a transfer_notification not sent by our own jetton wallet
    contract is a forgery and must never be cached as a real USDT credit."""
    client = MagicMock()
    forged = _chain_tx(
        lt=100, now=int(time.time()), src_addr="EQ_attacker_wallet",
        body=_notification_body(amount=9_999_999, nonce="forged:sid"),
    )
    client.get_transactions = AsyncMock(side_effect=[[forged], []])

    m = _monitor(client)
    await m._poll()

    assert m.get("forged:sid") is None


async def test_jetton_monitor_poll_skips_unresolvable_src():
    client = MagicMock()
    tx = _chain_tx(lt=100, now=int(time.time()), src_addr=None,
                   body=_notification_body(nonce="x:sid"))
    client.get_transactions = AsyncMock(side_effect=[[tx], []])

    m = _monitor(client)
    await m._poll()
    assert m.get("x:sid") is None


async def test_jetton_monitor_poll_skips_notification_without_nonce():
    client = MagicMock()
    tx = _chain_tx(lt=100, now=int(time.time()), src_addr=_JETTON_WALLET,
                   body=_notification_body(nonce=None))
    client.get_transactions = AsyncMock(side_effect=[[tx], []])

    m = _monitor(client)
    await m._poll()
    assert not m._by_nonce


async def test_jetton_monitor_poll_skips_non_notification_message():
    client = MagicMock()
    junk = begin_cell().store_uint(0xDEADBEEF, 32).end_cell()
    tx = _chain_tx(lt=100, now=int(time.time()), src_addr=_JETTON_WALLET, body=junk)
    client.get_transactions = AsyncMock(side_effect=[[tx], []])

    m = _monitor(client)
    await m._poll()
    assert not m._by_nonce


async def test_jetton_monitor_poll_stops_at_already_processed_lt():
    client = MagicMock()
    old = _chain_tx(lt=5, now=int(time.time()), src_addr=_JETTON_WALLET,
                    body=_notification_body(nonce="old:sid"))
    client.get_transactions = AsyncMock(side_effect=[[old], []])

    m = _monitor(client)
    m._last_processed_lt = 50  # we already saw everything up to lt=50
    await m._poll()
    assert m.get("old:sid") is None


async def test_jetton_monitor_poll_evicts_stale_cached_entries():
    client = MagicMock()
    client.get_transactions = AsyncMock(side_effect=[[]])

    m = _monitor(client)
    stale_ts = int(time.time()) - JettonWalletMonitor.CACHE_TTL - 1
    m._by_nonce["stale"] = _jpx(amount=1, sender="EQp", now_ts=stale_ts)
    m._by_nonce["fresh"] = _jpx(amount=1, sender="EQp", now_ts=int(time.time()))

    await m._poll()
    assert "stale" not in m._by_nonce
    assert "fresh" in m._by_nonce


async def test_jetton_monitor_poll_swallows_exceptions():
    client = MagicMock()
    client.get_transactions = AsyncMock(side_effect=RuntimeError("liteserver down"))
    m = _monitor(client)
    await m._poll()  # must not raise


async def test_jetton_monitor_force_wakes_loop_and_stop_exits():
    client = MagicMock()
    client.get_transactions = AsyncMock(return_value=[])

    m = _monitor(client, poll_interval=60)
    m._task = asyncio.create_task(m._loop())
    await asyncio.sleep(0.01)   # let the loop block on the force event
    m.force()
    await asyncio.sleep(0.01)
    await m.stop()
    assert m._task.done()
