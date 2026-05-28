"""Tests for verify.py — ProcessedTxStore, nonce parsing, WalletMonitor, PaymentVerifier."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import payments as verify_module
from transfer import payment_body, text_comment_body
from payments import (
    NonceMeta,
    PaymentVerificationError,
    PaymentVerifier,
    ProcessedTxStore,
    WalletMonitor,
    _parse_payment_nonce,
    parse_nonce,
)


# ── parse_nonce ────────────────────────────────────────────────────────

def test_parse_nonce_trims_whitespace():
    assert parse_nonce("  abc:sidecar  ").value == "abc:sidecar"


def test_parse_nonce_empty_string():
    assert parse_nonce("").value == ""


def test_parse_nonce_preserves_internal_colons():
    assert parse_nonce("a:b:c").value == "a:b:c"


# ── _parse_payment_nonce ───────────────────────────────────────────────

def test_parse_payment_nonce_none_returns_empty():
    assert _parse_payment_nonce(None) == ""


def test_parse_payment_nonce_wrong_opcode_returns_empty():
    # A plain text comment (opcode 0) must NOT be accepted as a payment comment —
    # this is part of the security contract (only real PAYMENT_OPCODE messages count).
    body = text_comment_body("some-nonce")
    assert _parse_payment_nonce(body) == ""


def test_parse_payment_nonce_valid_payment_body():
    body = payment_body("my-nonce:sidecar-1")
    assert _parse_payment_nonce(body) == "my-nonce:sidecar-1"


def test_parse_payment_nonce_truncated_body_returns_empty():
    from pytoniq_core import begin_cell
    # Only 16 bits of data — less than the 32 bits required for the opcode.
    short_cell = begin_cell().store_uint(0, 16).end_cell()
    assert _parse_payment_nonce(short_cell) == ""


# ── ProcessedTxStore ───────────────────────────────────────────────────

async def test_processed_tx_store_roundtrip(tmp_tx_db):
    store = ProcessedTxStore(tmp_tx_db)
    try:
        assert await store.is_processed("hash1") is False
        await store.mark_processed("hash1")
        assert await store.is_processed("hash1") is True
        assert await store.is_processed("hash2") is False
    finally:
        await store.close()
        # Wait for the background cleanup task created by mark_processed
        # to finish so pytest doesn't warn about pending tasks.
        await asyncio.sleep(0.05)


async def test_processed_tx_store_init_lazily_on_is_processed(tmp_tx_db):
    store = ProcessedTxStore(tmp_tx_db)
    # Directly call is_processed without init() — must work.
    try:
        assert await store.is_processed("never-seen") is False
    finally:
        await store.close()


async def test_processed_tx_store_mark_processed_twice_raises(tmp_tx_db):
    store = ProcessedTxStore(tmp_tx_db)
    try:
        await store.mark_processed("dup-hash")
        # Second insert violates the PRIMARY KEY constraint; this is the signal
        # the caller relies on to detect replays.
        with pytest.raises(Exception):
            await store.mark_processed("dup-hash")
    finally:
        await store.close()
        await asyncio.sleep(0.05)


async def test_processed_tx_store_cleanup_removes_old_entries(tmp_tx_db):
    store = ProcessedTxStore(tmp_tx_db)
    try:
        await store.init()
        # Insert a synthetic old row directly to bypass the "created_at = now()" path.
        old_iso = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        fresh_iso = datetime.now(timezone.utc).isoformat()
        await store._conn.execute(
            "INSERT INTO processed_txs (tx_hash, created_at) VALUES (?, ?)",
            ("old", old_iso),
        )
        await store._conn.execute(
            "INSERT INTO processed_txs (tx_hash, created_at) VALUES (?, ?)",
            ("fresh", fresh_iso),
        )
        await store._conn.commit()

        await store.cleanup(older_than_seconds=30 * 24 * 3600)

        assert await store.is_processed("old") is False
        assert await store.is_processed("fresh") is True
    finally:
        await store.close()


async def test_processed_tx_store_close_without_init_is_safe(tmp_tx_db):
    store = ProcessedTxStore(tmp_tx_db)
    await store.close()  # must not raise


# ── WalletMonitor ──────────────────────────────────────────────────────

def _mk_tx(lt: int, now: int, nonce: str | None) -> SimpleNamespace:
    if nonce is None:
        in_msg = None
    else:
        in_msg = SimpleNamespace(body=payment_body(nonce))
    return SimpleNamespace(lt=lt, now=now, in_msg=in_msg)


async def test_wallet_monitor_get_and_consume_trim_whitespace():
    monitor = WalletMonitor(client=MagicMock(), address="EQaddr")
    tx = _mk_tx(lt=10, now=int(time.time()), nonce="nonce-1")
    monitor._by_nonce["nonce-1"] = tx

    assert await monitor.get("  nonce-1  ") is tx
    assert await monitor.consume("nonce-1") is tx
    assert await monitor.get("nonce-1") is None


async def test_wallet_monitor_poll_caches_new_transactions():
    client = MagicMock()
    now_ts = int(time.time())
    tx_a = _mk_tx(lt=100, now=now_ts, nonce="nonce-a")
    tx_b = _mk_tx(lt=99, now=now_ts, nonce="nonce-b")
    tx_no_msg = _mk_tx(lt=98, now=now_ts, nonce=None)

    client.get_transactions = AsyncMock(side_effect=[[tx_a, tx_b, tx_no_msg], []])

    monitor = WalletMonitor(client=client, address="EQaddr")
    await monitor._poll()

    assert await monitor.get("nonce-a") is tx_a
    assert await monitor.get("nonce-b") is tx_b
    # The next poll starts from the highest LT we processed.
    assert monitor._last_processed_lt == 100


async def test_wallet_monitor_poll_skips_expired_txs():
    client = MagicMock()
    expired_ts = int(time.time()) - WalletMonitor.CACHE_TTL - 10
    tx_old = _mk_tx(lt=50, now=expired_ts, nonce="old-nonce")
    client.get_transactions = AsyncMock(return_value=[tx_old])

    monitor = WalletMonitor(client=client, address="EQaddr")
    await monitor._poll()
    assert await monitor.get("old-nonce") is None


async def test_wallet_monitor_poll_evicts_stale_cached_entries():
    client = MagicMock()
    client.get_transactions = AsyncMock(return_value=[])

    monitor = WalletMonitor(client=client, address="EQaddr")
    stale_ts = int(time.time()) - WalletMonitor.CACHE_TTL - 1
    monitor._by_nonce["stale"] = SimpleNamespace(now=stale_ts)
    monitor._by_nonce["fresh"] = SimpleNamespace(now=int(time.time()))

    await monitor._poll()
    assert "stale" not in monitor._by_nonce
    assert "fresh" in monitor._by_nonce


async def test_wallet_monitor_poll_swallows_exceptions():
    client = MagicMock()
    client.get_transactions = AsyncMock(side_effect=RuntimeError("liteserver down"))
    monitor = WalletMonitor(client=client, address="EQaddr")
    await monitor._poll()  # must not raise


async def test_wallet_monitor_force_wakes_loop_and_stop_exits():
    client = MagicMock()
    client.get_transactions = AsyncMock(return_value=[])

    monitor = WalletMonitor(client=client, address="EQaddr", poll_interval=60)
    monitor._task = asyncio.create_task(monitor._loop())
    task = monitor._task
    # Let the loop start and block on the force event
    await asyncio.sleep(0.01)
    monitor.force()
    await asyncio.sleep(0.01)
    await monitor.stop()
    # stop() nulls _task after awaiting it (so replace_client can rebind cleanly)
    assert monitor._task is None
    assert task.done()


# ── PaymentVerifier ────────────────────────────────────────────────────

def _mk_verified_tx(*, sender: str, amount: int, now_ts: int, nonce: str, hash_hex: str = "aa" * 32):
    src = MagicMock()
    src.to_str = MagicMock(return_value=sender)
    value = SimpleNamespace(grams=amount)
    in_msg = SimpleNamespace(
        info=SimpleNamespace(src=src, value=value),
        body=payment_body(nonce),
    )
    cell = MagicMock()
    cell.hash = bytes.fromhex(hash_hex)
    return SimpleNamespace(lt=1, now=now_ts, in_msg=in_msg, cell=cell)


async def test_payment_verifier_raises_when_not_started():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1000, payment_timeout_seconds=300)
    with pytest.raises(RuntimeError, match="not started"):
        await v.verify("tx", "nonce")


async def test_payment_verifier_success():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1000, payment_timeout_seconds=300)
    tx = _mk_verified_tx(
        sender="EQsender", amount=5000, now_ts=int(time.time()),
        nonce="abc:sidecar", hash_hex="bb" * 32,
    )
    monitor = MagicMock()
    monitor.get = AsyncMock(return_value=tx)
    monitor.consume = AsyncMock(return_value=tx)
    v._monitor = monitor

    result = await v.verify(tx_hash="user-supplied", raw_nonce="abc:sidecar")
    # CRITICAL security check: the returned tx_hash is the real on-chain hash,
    # NOT the user-supplied one — otherwise a caller could replay fake hashes.
    assert result.tx_hash == "bb" * 32
    assert result.tx_hash != "user-supplied"
    assert result.sender == "EQsender"
    assert result.amount == 5000
    monitor.consume.assert_called_once_with("abc:sidecar")


async def test_payment_verifier_amount_below_min_rejected():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=10_000, payment_timeout_seconds=300)
    tx = _mk_verified_tx(sender="EQsender", amount=9_999, now_ts=int(time.time()), nonce="n")
    v._monitor = MagicMock()
    v._monitor.get = AsyncMock(return_value=tx)
    v._monitor.consume = AsyncMock()

    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await v.verify("tx", "n")


async def test_payment_verifier_min_amount_override():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1_000, payment_timeout_seconds=300)
    tx = _mk_verified_tx(sender="EQsender", amount=5_000, now_ts=int(time.time()), nonce="n")
    v._monitor = MagicMock()
    v._monitor.get = AsyncMock(return_value=tx)
    v._monitor.consume = AsyncMock()

    # Override with higher threshold — should reject.
    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await v.verify("tx", "n", min_amount=10_000)


async def test_payment_verifier_session_expired():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1000, payment_timeout_seconds=60)
    stale_ts = int(time.time()) - 120
    tx = _mk_verified_tx(sender="EQsender", amount=5000, now_ts=stale_ts, nonce="n")
    v._monitor = MagicMock()
    v._monitor.get = AsyncMock(return_value=tx)
    v._monitor.consume = AsyncMock()

    with pytest.raises(PaymentVerificationError, match="session expired"):
        await v.verify("tx", "n")


async def test_payment_verifier_missing_sender_rejected():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1000, payment_timeout_seconds=300)
    tx = _mk_verified_tx(sender="EQsender", amount=5000, now_ts=int(time.time()), nonce="n")
    # Force sender extraction to fail.
    tx.in_msg.info.src.to_str = MagicMock(side_effect=RuntimeError("bad addr"))
    v._monitor = MagicMock()
    v._monitor.get = AsyncMock(return_value=tx)
    v._monitor.consume = AsyncMock()

    with pytest.raises(PaymentVerificationError, match="sender is missing"):
        await v.verify("tx", "n")


async def test_payment_verifier_timeout_when_tx_never_appears(monkeypatch):
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1000, payment_timeout_seconds=300)
    monitor = MagicMock()
    monitor.get = AsyncMock(return_value=None)
    monitor.force = MagicMock()
    v._monitor = monitor

    # Speed up: shrink the wait window to a few poll cycles.
    monkeypatch.setattr(PaymentVerifier, "VERIFY_TIMEOUT", 0.05)
    monkeypatch.setattr(PaymentVerifier, "VERIFY_POLL", 0.01)

    with pytest.raises(PaymentVerificationError, match="not found"):
        await v.verify("tx", "n")
    # The verifier must have forced at least one poll while waiting.
    assert monitor.force.called


async def test_payment_verifier_amount_extraction_failure_defaults_to_zero():
    v = PaymentVerifier(agent_wallet="EQw", min_amount=1000, payment_timeout_seconds=300)
    tx = _mk_verified_tx(sender="EQsender", amount=5000, now_ts=int(time.time()), nonce="n")
    # Break amount extraction: `int(...)` on a non-numeric will raise inside the try.
    tx.in_msg.info.value = SimpleNamespace(grams="not-a-number")
    v._monitor = MagicMock()
    v._monitor.get = AsyncMock(return_value=tx)
    v._monitor.consume = AsyncMock()

    with pytest.raises(PaymentVerificationError, match="lower than required"):
        await v.verify("tx", "n")
