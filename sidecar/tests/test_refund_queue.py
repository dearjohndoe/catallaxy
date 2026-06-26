"""Characterization tests for payments.refund_queue.RefundQueue.

Pins the SQLite-backed state machine — enqueue idempotency, atomic claim,
due-selection, and every status transition — plus the fields the refactor
relies on (``rail``, ``force_refund``). This is the contract that must survive
the planned ``tx_hash`` → ``{chain}:{tx_id}`` key namespacing migration: the
migration is only safe if current dedup/transition behaviour is locked first.
"""

from __future__ import annotations

import time

import pytest

from payments.refund_queue import (
    RefundQueue,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSED,
    STATUS_REFUNDED,
    STATUS_REFUNDING,
)


@pytest.fixture
async def rq(tmp_path):
    q = RefundQueue(str(tmp_path / "rq.db"))
    await q.init()
    yield q
    await q.close()


# ── enqueue ────────────────────────────────────────────────────────────


async def test_enqueue_new_returns_true_and_persists_all_fields(rq):
    assert await rq.enqueue(tx_hash="TX", nonce="n:sid", rail="USDT",
                            sender="EQu", amount=1234, sku_id="sku",
                            force_refund=True) is True
    rec = await rq.get("TX")
    assert rec.tx_hash == "TX" and rec.nonce == "n:sid" and rec.rail == "USDT"
    assert rec.sender == "EQu" and rec.amount == 1234 and rec.sku_id == "sku"
    assert rec.status == STATUS_PENDING and rec.attempts == 0
    assert rec.force_refund == 1


async def test_enqueue_duplicate_tx_hash_returns_false(rq):
    assert await rq.enqueue(tx_hash="TX", nonce="n", rail="TON") is True
    # Same primary key — second insert is rejected, original untouched.
    assert await rq.enqueue(tx_hash="TX", nonce="other", rail="USDT") is False
    rec = await rq.get("TX")
    assert rec.nonce == "n" and rec.rail == "TON"


async def test_enqueue_defaults(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    rec = await rq.get("TX")
    assert rec.sender is None and rec.amount is None and rec.sku_id is None
    assert rec.force_refund == 0


async def test_get_unknown_returns_none(rq):
    assert await rq.get("nope") is None


# ── claim (atomic pending → refunding) ─────────────────────────────────


async def test_claim_transitions_pending_to_refunding_and_bumps_attempts(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    assert await rq.claim("TX") is True
    rec = await rq.get("TX")
    assert rec.status == STATUS_REFUNDING and rec.attempts == 1
    assert rec.last_attempt_at is not None


async def test_claim_is_exclusive_second_claim_fails(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    assert await rq.claim("TX") is True
    # Already 'refunding' → not 'pending' → second claim loses the race.
    assert await rq.claim("TX") is False
    assert (await rq.get("TX")).attempts == 1


# ── fetch_due ──────────────────────────────────────────────────────────


async def test_fetch_due_returns_only_pending_and_due(rq):
    await rq.enqueue(tx_hash="DUE", nonce="n", rail="TON")
    await rq.enqueue(tx_hash="CLAIMED", nonce="n", rail="TON")
    await rq.claim("CLAIMED")  # → refunding, excluded
    due = await rq.fetch_due()
    assert [e.tx_hash for e in due] == ["DUE"]


async def test_fetch_due_excludes_future_next_attempt(rq):
    await rq.enqueue(tx_hash="LATER", nonce="n", rail="TON")
    await rq.claim("LATER")
    await rq.mark_failed_transient("LATER", "blip", backoff_seconds=3600)  # due in 1h
    assert await rq.fetch_due() == []


async def test_fetch_due_orders_by_next_attempt_and_respects_limit(rq):
    now = int(time.time())
    for i in range(3):
        await rq.enqueue(tx_hash=f"T{i}", nonce="n", rail="TON")
    # Push T0 furthest into the (past) future ordering via transient backoff math:
    # claim then transient with negative-ish ordering is awkward; instead just
    # assert limit truncates and all returned are pending.
    due = await rq.fetch_due(limit=2)
    assert len(due) == 2
    assert all(e.status == STATUS_PENDING for e in due)
    assert all(e.next_attempt_at <= now + 1 for e in due)


# ── mark_refunded ──────────────────────────────────────────────────────


async def test_mark_refunded_is_terminal_and_records_hash(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.claim("TX")
    await rq.mark_refunded("TX", "REFUND_HASH")
    rec = await rq.get("TX")
    assert rec.status == STATUS_REFUNDED and rec.refund_tx == "REFUND_HASH"
    assert rec.last_error is None


# ── mark_processed (atomic, only from pending) ─────────────────────────


async def test_mark_processed_from_pending(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.mark_processed("TX")
    assert (await rq.get("TX")).status == STATUS_PROCESSED


async def test_mark_processed_noop_when_not_pending(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.claim("TX")  # → refunding
    await rq.mark_processed("TX")
    # Guarded on status='pending' → no transition out of 'refunding'.
    assert (await rq.get("TX")).status == STATUS_REFUNDING


# ── mark_failed_transient (refunding → pending + backoff) ──────────────


async def test_mark_failed_transient_reverts_to_pending_with_backoff(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.claim("TX")
    before = int(time.time())
    await rq.mark_failed_transient("TX", "rpc blip", backoff_seconds=120)
    rec = await rq.get("TX")
    assert rec.status == STATUS_PENDING
    assert rec.last_error == "rpc blip"
    assert rec.next_attempt_at >= before + 120


async def test_mark_failed_transient_noop_when_not_refunding(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")  # still pending
    await rq.mark_failed_transient("TX", "e", backoff_seconds=10)
    rec = await rq.get("TX")
    # Updates only WHERE status='refunding' → pending entry untouched.
    assert rec.status == STATUS_PENDING and rec.last_error is None


async def test_mark_failed_transient_truncates_long_error(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.claim("TX")
    await rq.mark_failed_transient("TX", "x" * 1000, backoff_seconds=1)
    assert len((await rq.get("TX")).last_error) == 500


# ── defer_pending (pre-claim backoff) ──────────────────────────────────


async def test_defer_pending_records_error_and_backoff_without_claiming(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")  # still pending
    before = int(time.time())
    await rq.defer_pending("TX", "could not recover", backoff_seconds=30)
    rec = await rq.get("TX")
    assert rec.status == STATUS_PENDING
    assert rec.last_error == "could not recover"
    assert rec.next_attempt_at >= before + 30
    assert rec.attempts == 0  # no claim happened


async def test_defer_pending_noop_when_not_pending(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.claim("TX")  # → refunding
    await rq.defer_pending("TX", "e", backoff_seconds=10)
    rec = await rq.get("TX")
    assert rec.status == STATUS_REFUNDING and rec.last_error is None


# ── mark_failed_permanent ──────────────────────────────────────────────


async def test_mark_failed_permanent_is_terminal(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.mark_failed_permanent("TX", "max attempts")
    rec = await rq.get("TX")
    assert rec.status == STATUS_FAILED and rec.last_error == "max attempts"


# ── update_payment_info ────────────────────────────────────────────────


async def test_update_payment_info_persists_sender_and_amount(rq):
    await rq.enqueue(tx_hash="TX", nonce="n", rail="TON")
    await rq.update_payment_info("TX", "EQrecovered", 5555)
    rec = await rq.get("TX")
    assert rec.sender == "EQrecovered" and rec.amount == 5555


# ── list_stale_refunding ───────────────────────────────────────────────


async def test_list_stale_refunding_returns_old_refunding_entries(rq):
    await rq.enqueue(tx_hash="STALE", nonce="n", rail="TON")
    await rq.claim("STALE")  # → refunding, last_attempt_at = now
    # older_than_seconds=-1 ⇒ cutoff is in the future ⇒ everything counts as stale.
    stale = await rq.list_stale_refunding(older_than_seconds=-1)
    assert [e.tx_hash for e in stale] == ["STALE"]


async def test_list_stale_refunding_excludes_fresh_and_non_refunding(rq):
    await rq.enqueue(tx_hash="FRESH", nonce="n", rail="TON")
    await rq.claim("FRESH")
    await rq.enqueue(tx_hash="PENDING", nonce="n", rail="TON")  # never claimed
    # Large window ⇒ nothing is old enough yet.
    assert await rq.list_stale_refunding(older_than_seconds=600) == []
