"""Tests for the tonapi-relay client classes used in remote-monitor mode."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chains.ton.remote_monitor import (
    RemoteJettonWalletMonitor,
    RemoteWalletMonitor,
    _RelayClient,
    _RetryableSubscribeError,
    _UNHEALTHY_CHECK_INTERVAL,
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
async def test_remote_monitor_get_is_single_shot_no_internal_retry():
    # get() does NOT retry internally — verify()'s deadline loop owns retries.
    # A single miss returns None immediately after one relay call.
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(return_value=None)
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("miss")
    assert tx is None
    assert relay.fetch_by_nonce.await_count == 1


@pytest.mark.asyncio
async def test_remote_monitor_get_hit_on_first_call():
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(return_value=_relay_payload(nonce="hit"))
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("hit")
    assert tx is not None
    assert relay.fetch_by_nonce.await_count == 1


@pytest.mark.asyncio
async def test_remote_monitor_consume_pops_from_cache():
    relay = MagicMock()
    relay.fetch_by_nonce = AsyncMock(return_value=_relay_payload(nonce="x"))
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    tx = await m.get("x")
    assert tx is not None
    consumed = await m.consume("x")
    assert consumed is tx
    # Cache is now empty — next get() round-trips to relay again (single-shot).
    relay.fetch_by_nonce.reset_mock()
    relay.fetch_by_nonce.return_value = None
    again = await m.get("x")
    assert again is None
    assert relay.fetch_by_nonce.await_count == 1


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


def _client(**kwargs) -> _RelayClient:
    defaults = dict(
        subscribe_budget=0.2,
        subscribe_initial_delay=0.01,
        subscribe_max_delay=0.04,
    )
    defaults.update(kwargs)
    return _RelayClient("http://relay.test", **defaults)


@pytest.mark.asyncio
async def test_subscribe_retries_then_succeeds():
    client = _client()
    calls = {"n": 0}

    async def once(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RetryableSubscribeError("refused")
        return {"ok": True}

    client._subscribe_once = once  # type: ignore[method-assign]
    result = await client.subscribe(None, None, None)
    assert result == {"ok": True}
    assert calls["n"] == 3
    assert client.is_subscribed is True
    await client.close()


@pytest.mark.asyncio
async def test_subscribe_success_first_try_does_not_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("chains.ton.remote_monitor.asyncio.sleep", fake_sleep)
    client = _client()

    async def once(*_a, **_k):
        return {"ok": True}

    client._subscribe_once = once  # type: ignore[method-assign]
    await client.subscribe(None, None, None)
    assert slept == []
    assert client.is_subscribed is True
    await client.close()


@pytest.mark.asyncio
async def test_subscribe_budget_exhausted_raises():
    client = _client(subscribe_budget=0.05)

    async def once(*_a, **_k):
        raise _RetryableSubscribeError("refused")

    client._subscribe_once = once  # type: ignore[method-assign]
    with pytest.raises(_RetryableSubscribeError, match="refused"):
        await client.subscribe(None, None, None)
    assert client.is_subscribed is False
    await client.close()


@pytest.mark.asyncio
async def test_subscribe_4xx_does_not_retry():
    client = _client()
    calls = {"n": 0}

    async def once(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("relay /subscribe HTTP 400: bad wallet")

    client._subscribe_once = once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="400"):
        await client.subscribe(None, None, None)
    assert calls["n"] == 1
    assert client.is_subscribed is False
    await client.close()


@pytest.mark.asyncio
async def test_subscribe_or_keep_trying_recovers_in_background():
    client = _client(subscribe_budget=0.04, subscribe_initial_delay=0.01, subscribe_max_delay=0.02)
    allow = asyncio.Event()

    async def once(*_a, **_k):
        if not allow.is_set():
            raise _RetryableSubscribeError("refused")
        return {"ok": True}

    client._subscribe_once = once  # type: ignore[method-assign]
    try:
        await client.subscribe_or_keep_trying("EQagent", None, "lbl")
        assert client.is_subscribed is False
        assert client._resubscribe_task is not None
        allow.set()
        for _ in range(80):
            if client.is_subscribed:
                break
            await asyncio.sleep(0.02)
        assert client.is_subscribed is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_is_healthy_false_until_subscribed():
    relay = MagicMock()
    relay.is_subscribed = False
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    assert m.is_healthy() is False
    relay.is_subscribed = True
    # Optimistic cache still True until the first refresh lands.
    assert m.is_healthy() is True


@pytest.mark.asyncio
async def test_is_healthy_holds_strong_ref_until_refresh_completes():
    relay = MagicMock()
    relay.is_subscribed = True
    started = asyncio.Event()

    async def slow_health() -> dict:
        started.set()
        await asyncio.sleep(0.05)
        return {"last_sync_at": 1}

    relay.health = slow_health
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    m._health_cache = (0.0, False)
    assert m.is_healthy() is False
    task = m._pending_refresh
    assert task is not None
    await started.wait()
    assert not task.done()
    await task
    assert m._health_cache[1] is True
    await m.stop()


@pytest.mark.asyncio
async def test_unhealthy_cache_expires_quickly():
    relay = MagicMock()
    relay.is_subscribed = True
    relay.health = AsyncMock(return_value={"last_sync_at": 1})
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    now = time.time()
    m._health_cache = (now, False)
    assert m.is_healthy() is False
    assert m._pending_refresh is None
    m._health_cache = (now - (_UNHEALTHY_CHECK_INTERVAL + 0.5), False)
    assert m.is_healthy() is False
    assert m._pending_refresh is not None
    await m._pending_refresh
    assert m._health_cache[1] is True
    await m.stop()


@pytest.mark.asyncio
async def test_healthy_cache_does_not_reschedule_within_interval():
    relay = MagicMock()
    relay.is_subscribed = True
    m = RemoteWalletMonitor(relay, account_id="0:agent")
    m._health_cache = (time.time(), True)
    assert m.is_healthy() is True
    assert m._pending_refresh is None
