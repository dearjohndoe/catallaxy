"""Tests for the tonapi-relay client classes used in remote-monitor mode."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from payments.remote_monitor import (
    RemoteJettonWalletMonitor,
    RemoteWalletMonitor,
    _wrap_jetton_entry,
    _wrap_ton_tx,
)


def _relay_payload(rail="TON", nonce="n1", amount=50000000, sender="EQsender"):
    return {
        "tx_hash": "ab" * 32,
        "account_id": "0:agent",
        "lt": 100,
        "utime": int(time.time()),
        "sender": sender,
        "amount": amount,
        "nonce": nonce,
        "rail": rail,
        "source": "webhook",
    }


def test_wrap_ton_tx_provides_verify_required_fields():
    data = _relay_payload(rail="TON")
    wrapped = _wrap_ton_tx(data)
    assert wrapped.now == data["utime"]
    assert wrapped.in_msg.info.value.grams == 50000000
    assert wrapped.in_msg.info.src.to_str(is_user_friendly=True) == "EQsender"
    assert wrapped.cell.hash.hex() == "ab" * 32
    # body=None must not break verify's _parse_payment_nonce (it returns "")
    assert wrapped.in_msg.body is None


def test_wrap_jetton_entry_has_jetton_payment_tx_shape():
    data = _relay_payload(rail="USDT", amount=70000, sender="EQjsender", nonce="n2")
    entry = _wrap_jetton_entry(data)
    assert entry.amount == 70000
    assert entry.sender == "EQjsender"
    assert entry.nonce == "n2"
    assert entry.tx.now == data["utime"]
    assert entry.tx.cell.hash.hex() == "ab" * 32


@pytest.mark.asyncio
async def test_remote_monitor_get_returns_cached_on_hit():
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(return_value=_relay_payload(nonce="hit"))
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("hit")
    assert tx is not None
    assert tx.in_msg.info.value.grams == 50000000
    # Second call should hit local cache, not relay.
    relay.fetch_by_nonce.reset_mock()
    again = await m.get("hit")
    assert again is tx
    relay.fetch_by_nonce.assert_not_called()


@pytest.mark.asyncio
async def test_remote_monitor_get_retries_three_times_with_sleep(monkeypatch):
    # Simulate two misses then a hit; check we wait between attempts.
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(
        side_effect=[None, None, _relay_payload(nonce="late")],
    )
    sleeps: list[float] = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("payments.remote_monitor.asyncio.sleep", fake_sleep)

    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("late")
    assert tx is not None
    # Two sleeps between three attempts
    assert sleeps == [3.0, 3.0]
    assert relay.fetch_by_nonce.await_count == 3


@pytest.mark.asyncio
async def test_remote_monitor_get_returns_none_after_three_misses(monkeypatch):
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(return_value=None)

    async def fake_sleep(sec):
        pass

    monkeypatch.setattr("payments.remote_monitor.asyncio.sleep", fake_sleep)

    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("never")
    assert tx is None
    assert relay.fetch_by_nonce.await_count == 3


@pytest.mark.asyncio
async def test_remote_monitor_consume_pops_from_cache():
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(return_value=_relay_payload(nonce="x"))
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("x")
    assert tx is not None
    consumed = await m.consume("x")
    assert consumed is tx
    # Cache is now empty — next get() will round-trip to relay again.
    relay.fetch_by_nonce.reset_mock()
    relay.fetch_by_nonce.side_effect = [None, None, None]

    async def fake_sleep(sec): pass
    import payments.remote_monitor as rm
    orig = rm.asyncio.sleep
    rm.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        again = await m.get("x")
    finally:
        rm.asyncio.sleep = orig  # type: ignore[assignment]
    assert again is None


@pytest.mark.asyncio
async def test_remote_jetton_monitor_wraps_into_jetton_payment_tx():
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(
        return_value=_relay_payload(rail="USDT", amount=70000, sender="EQj", nonce="j"),
    )
    m = RemoteJettonWalletMonitor(relay, account_id="0:jetton_wallet")
    entry = await m.get("j")
    assert entry is not None
    assert entry.amount == 70000
    assert entry.sender == "EQj"
    assert entry.nonce == "j"
    # Cell.hash → bytes; verify() does .hex() on it.
    assert entry.tx.cell.hash.hex() == "ab" * 32


def test_remote_monitor_force_and_replace_client_are_noop():
    relay = MagicMock()
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    # No exception, no side effects.
    m.force()
    # replace_client is async — make sure it doesn't blow up.
    asyncio.run(m.replace_client(object()))


def test_remote_monitor_is_healthy_returns_cached_true_initially():
    relay = MagicMock()
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    # No event loop running here — should just return True without scheduling refresh.
    assert m.is_healthy() is True
