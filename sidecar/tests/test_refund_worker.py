"""Characterization tests for api.domain.refund_worker.

These pin the refund background-worker state machine and its per-rail dispatch
*before* the multichain refactor (which replaces the ``entry.rail == "USDT"``
string branching with ChainRail-object dispatch). Behaviour must stay
bit-for-bit identical across that move, so every branch of ``_process_entry``
and ``_recover_payment_info`` is locked here.

On-chain touchpoints (``refund_user``, ``find_existing_refund_tx``,
``_check_balance_for_refund``, ``_acquire_lite_client``) are patched at the
module level — these tests are about the worker's control flow, not RPC.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.domain import refund_worker
from api.domain.refund_worker import (
    _backoff_for_attempt,
    _BACKOFF_SCHEDULE,
    _process_entry,
    _recover_stale_refunding,
    _tick,
)
from payments.processed_tx import ProcessedTxStore
from payments.refund_queue import RefundQueue


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def worker_app(tmp_path):
    """Minimal SidecarApp stand-in with real RefundQueue + ProcessedTxStore."""
    rq = RefundQueue(str(tmp_path / "refunds.db"))
    txs = ProcessedTxStore(str(tmp_path / "ptx.db"))
    await rq.init()
    await txs.init()
    app = SimpleNamespace(
        refund_queue=rq,
        tx_store=txs,
        settings=SimpleNamespace(
            refund_max_attempts=10,
            refund_fee_nanoton=500_000,
            agent_wallet="EQagent",
            testnet=True,
        ),
        sidecar_id="sid-test",
        sender=MagicMock(),
        _agent_jetton_wallet="EQjw",
        verifier=None,
        jetton_verifier=None,
        owner_bot=None,
        ensure_jetton_verifier=AsyncMock(return_value=False),
        # Single refund dispatch point — the worker now calls app.refund_user,
        # which delegates to the rail. Mocked here; rail.refund parity lives in
        # tests/test_rails_ton.py.
        refund_user=AsyncMock(return_value="REFUND_TX"),
    )
    yield app
    await rq.close()
    await txs.close()


@pytest.fixture
def patch_onchain(monkeypatch, worker_app):
    """Patch the worker's on-chain helpers to safe, controllable stubs.

    Returns the mocks so a test can tweak return values/side-effects. The
    refund mock is ``worker_app.refund_user`` (the worker's dispatch point).
    """
    refund_user = worker_app.refund_user  # AsyncMock from the worker_app fixture
    balance = AsyncMock(return_value=(True, ""))
    find_existing = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _fake_client(app):
        yield object()

    monkeypatch.setattr(refund_worker, "_check_balance_for_refund", balance)
    monkeypatch.setattr(refund_worker, "find_existing_refund_tx", find_existing)
    monkeypatch.setattr(refund_worker, "_acquire_lite_client", lambda app: _fake_client(app))
    return SimpleNamespace(refund_user=refund_user, balance=balance, find_existing=find_existing)


@asynccontextmanager
async def _noop_client():
    yield object()


async def _entry(rq: RefundQueue, tx_hash: str, **kw):
    await rq.enqueue(tx_hash=tx_hash, nonce=kw.get("nonce", f"n:{tx_hash}"),
                     rail=kw.get("rail", "TON"), sender=kw.get("sender"),
                     amount=kw.get("amount"), sku_id=kw.get("sku_id"),
                     force_refund=kw.get("force_refund", False))
    return await rq.get(tx_hash)


# ── _backoff_for_attempt (pure) ────────────────────────────────────────


def test_backoff_clamps_low_attempts_to_first_step():
    # attempts 0 and 1 both map to schedule[0] (max(attempts-1, 0)).
    assert _backoff_for_attempt(0) == _BACKOFF_SCHEDULE[0] == 30
    assert _backoff_for_attempt(1) == 30


def test_backoff_progresses_with_attempts():
    assert _backoff_for_attempt(2) == _BACKOFF_SCHEDULE[1] == 60
    assert _backoff_for_attempt(3) == _BACKOFF_SCHEDULE[2] == 120


def test_backoff_clamps_high_attempts_to_last_step():
    assert _backoff_for_attempt(999) == _BACKOFF_SCHEDULE[-1] == 86400


# ── _process_entry: race-guard on already-processed tx ─────────────────


async def test_process_entry_skips_refund_when_tx_already_processed(worker_app, patch_onchain):
    await worker_app.tx_store.mark_processed("TX1")
    entry = await _entry(worker_app.refund_queue, "TX1", sender="EQu", amount=1000)

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_not_awaited()
    assert (await worker_app.refund_queue.get("TX1")).status == "processed"


async def test_process_entry_force_refund_bypasses_processed_guard(worker_app, patch_onchain):
    await worker_app.tx_store.mark_processed("TX1")
    entry = await _entry(worker_app.refund_queue, "TX1", sender="EQu", amount=1000,
                         force_refund=True)

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_awaited_once()
    rec = await worker_app.refund_queue.get("TX1")
    assert rec.status == "refunded" and rec.refund_tx == "REFUND_TX"


# ── _process_entry: dedup probe on retry ───────────────────────────────


async def test_process_entry_dedup_picks_up_prior_onchain_refund(worker_app, patch_onchain):
    # An entry that was already attempted once (attempts>=1) with known
    # sender/amount triggers a pre-send probe; if a refund is already on-chain
    # we adopt it instead of double-sending.
    rq = worker_app.refund_queue
    await rq.enqueue(tx_hash="TX2", nonce="n", rail="TON", sender="EQu", amount=1000)
    await rq.claim("TX2")                       # → refunding, attempts=1
    await rq.mark_failed_transient("TX2", "blip", 0)  # → pending, attempts stays 1
    entry = await rq.get("TX2")
    assert entry.attempts == 1
    patch_onchain.find_existing.return_value = "PRIOR_REFUND"

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_not_awaited()
    rec = await rq.get("TX2")
    assert rec.status == "refunded" and rec.refund_tx == "PRIOR_REFUND"


# ── _process_entry: permanent give-up ──────────────────────────────────


async def test_process_entry_permanent_failure_after_max_attempts(worker_app, patch_onchain):
    worker_app.settings.refund_max_attempts = 1
    # No sender/amount → dedup probe skipped, max-attempts branch reached.
    await worker_app.refund_queue.enqueue(tx_hash="TX3", nonce="n", rail="TON")
    await worker_app.refund_queue.claim("TX3")                  # attempts=1
    await worker_app.refund_queue.mark_failed_transient("TX3", "e", 0)
    entry = await worker_app.refund_queue.get("TX3")

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_not_awaited()
    rec = await worker_app.refund_queue.get("TX3")
    assert rec.status == "failed"
    assert "max_attempts_exceeded" in rec.last_error


async def test_process_entry_permanent_failure_notifies_owner(worker_app, patch_onchain):
    worker_app.settings.refund_max_attempts = 1
    worker_app.owner_bot = MagicMock()
    await worker_app.refund_queue.enqueue(tx_hash="TX3b", nonce="n", rail="TON")
    await worker_app.refund_queue.claim("TX3b")
    await worker_app.refund_queue.mark_failed_transient("TX3b", "e", 0)
    entry = await worker_app.refund_queue.get("TX3b")

    await _process_entry(worker_app, entry)

    worker_app.owner_bot.notify_refund.assert_called_once()


# ── _process_entry: recover sender/amount ──────────────────────────────


async def test_process_entry_transient_when_recover_fails(worker_app, patch_onchain, monkeypatch):
    monkeypatch.setattr(refund_worker, "_recover_payment_info", AsyncMock(return_value=False))
    entry = await _entry(worker_app.refund_queue, "TX4")  # no sender/amount

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_not_awaited()
    rec = await worker_app.refund_queue.get("TX4")
    # Pre-claim failure is deferred (defer_pending): entry stays 'pending' but
    # the error is recorded and the retry is backed off (next_attempt_at > now).
    assert rec.status == "pending"
    assert "could not recover" in rec.last_error
    assert rec.next_attempt_at > rec.created_at


# ── _process_entry: balance gate ───────────────────────────────────────


async def test_process_entry_transient_when_balance_insufficient(worker_app, patch_onchain):
    patch_onchain.balance.return_value = (False, "TON balance 1 < required 1000")
    entry = await _entry(worker_app.refund_queue, "TX5", sender="EQu", amount=1000)

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_not_awaited()
    rec = await worker_app.refund_queue.get("TX5")
    # Pre-claim failure deferred with backoff (defer_pending); refund skipped.
    assert rec.status == "pending"
    assert "balance_check_failed" in rec.last_error
    assert rec.next_attempt_at > rec.created_at


# ── _process_entry: claim race lost ────────────────────────────────────


async def test_process_entry_noop_when_claim_lost(worker_app, patch_onchain):
    entry = await _entry(worker_app.refund_queue, "TX6", sender="EQu", amount=1000)
    # Another worker already claimed it → status 'refunding', our claim fails.
    await worker_app.refund_queue.claim("TX6")
    entry = await worker_app.refund_queue.get("TX6")  # attempts=1, still has sender/amount
    patch_onchain.find_existing.return_value = None   # dedup probe finds nothing

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_not_awaited()
    assert (await worker_app.refund_queue.get("TX6")).status == "refunding"


# ── _process_entry: successful refund ──────────────────────────────────


async def test_process_entry_success_marks_refunded_and_notifies(worker_app, patch_onchain):
    worker_app.owner_bot = MagicMock()
    entry = await _entry(worker_app.refund_queue, "TX7", sender="EQu", amount=1000)

    await _process_entry(worker_app, entry)

    patch_onchain.refund_user.assert_awaited_once()
    rec = await worker_app.refund_queue.get("TX7")
    assert rec.status == "refunded" and rec.refund_tx == "REFUND_TX"
    kwargs = worker_app.owner_bot.notify_refund.call_args.kwargs
    assert kwargs["status"] == "refunded" and kwargs["refund_tx"] == "REFUND_TX"


async def test_process_entry_passes_rail_through_to_refund_user(worker_app, patch_onchain):
    entry = await _entry(worker_app.refund_queue, "TX7u", rail="USDT",
                         sender="EQu", amount=1_000_000)

    await _process_entry(worker_app, entry)

    assert patch_onchain.refund_user.call_args.kwargs["rail"] == "USDT"


async def test_process_entry_transient_when_refund_send_raises(worker_app, patch_onchain):
    patch_onchain.refund_user.side_effect = RuntimeError("liteserver down")
    entry = await _entry(worker_app.refund_queue, "TX8", sender="EQu", amount=1000)

    await _process_entry(worker_app, entry)

    rec = await worker_app.refund_queue.get("TX8")
    assert rec.status == "pending"
    assert "send error" in rec.last_error


async def test_process_entry_permanent_when_refund_returns_none(worker_app, patch_onchain):
    # refund_user returns None ⇒ amount-after-fee <= 0 ⇒ permanent, no retry.
    patch_onchain.refund_user.return_value = None
    entry = await _entry(worker_app.refund_queue, "TX9", sender="EQu", amount=1)

    await _process_entry(worker_app, entry)

    rec = await worker_app.refund_queue.get("TX9")
    assert rec.status == "failed"
    assert "returned None" in rec.last_error


# ── _recover_payment_info: per-rail dispatch ───────────────────────────


async def test_recover_unknown_rail_returns_false(worker_app):
    entry = await _entry(worker_app.refund_queue, "TXR0", rail="SOL")
    assert await refund_worker._recover_payment_info(worker_app, entry) is False


async def test_recover_usdt_returns_false_when_verifier_unavailable(worker_app):
    worker_app.ensure_jetton_verifier = AsyncMock(return_value=False)
    entry = await _entry(worker_app.refund_queue, "TXR1", rail="USDT")
    assert await refund_worker._recover_payment_info(worker_app, entry) is False


async def test_recover_ton_returns_false_when_no_monitor(worker_app):
    worker_app.verifier = None
    entry = await _entry(worker_app.refund_queue, "TXR2", rail="TON")
    assert await refund_worker._recover_payment_info(worker_app, entry) is False


async def test_recover_usdt_updates_payment_info_from_monitor(worker_app, monkeypatch):
    monkeypatch.setattr(refund_worker.asyncio, "sleep", AsyncMock())
    monitor = SimpleNamespace(
        force=MagicMock(),
        get=AsyncMock(return_value=SimpleNamespace(sender="EQfound", amount=777)),
    )
    worker_app.ensure_jetton_verifier = AsyncMock(return_value=True)
    worker_app.jetton_verifier = SimpleNamespace(_monitor=monitor)
    entry = await _entry(worker_app.refund_queue, "TXR3", rail="USDT", nonce="abc:sid-test")

    ok = await refund_worker._recover_payment_info(worker_app, entry)

    assert ok is True
    rec = await worker_app.refund_queue.get("TXR3")
    assert rec.sender == "EQfound" and rec.amount == 777


async def test_recover_ton_updates_payment_info_from_monitor(worker_app, monkeypatch):
    monkeypatch.setattr(refund_worker.asyncio, "sleep", AsyncMock())
    src = SimpleNamespace(to_str=lambda **kw: "EQton")
    tx = SimpleNamespace(in_msg=SimpleNamespace(info=SimpleNamespace(
        src=src, value=SimpleNamespace(grams=4242))))
    monitor = SimpleNamespace(force=MagicMock(), get=AsyncMock(return_value=tx))
    worker_app.verifier = SimpleNamespace(_monitor=monitor)
    entry = await _entry(worker_app.refund_queue, "TXR4", rail="TON", nonce="abc:sid-test")

    ok = await refund_worker._recover_payment_info(worker_app, entry)

    assert ok is True
    rec = await worker_app.refund_queue.get("TXR4")
    assert rec.sender == "EQton" and rec.amount == 4242


async def test_recover_usdt_returns_false_when_monitor_misses_tx(worker_app, monkeypatch):
    monkeypatch.setattr(refund_worker.asyncio, "sleep", AsyncMock())
    monitor = SimpleNamespace(force=MagicMock(), get=AsyncMock(return_value=None))
    worker_app.ensure_jetton_verifier = AsyncMock(return_value=True)
    worker_app.jetton_verifier = SimpleNamespace(_monitor=monitor)
    entry = await _entry(worker_app.refund_queue, "TXR5", rail="USDT", nonce="abc:sid-test")
    assert await refund_worker._recover_payment_info(worker_app, entry) is False


async def test_recover_ton_returns_false_when_amount_extraction_raises(worker_app, monkeypatch):
    monkeypatch.setattr(refund_worker.asyncio, "sleep", AsyncMock())
    bad_tx = SimpleNamespace(in_msg=SimpleNamespace(info=SimpleNamespace(src=None, value=None)))
    monitor = SimpleNamespace(force=MagicMock(), get=AsyncMock(return_value=bad_tx))
    worker_app.verifier = SimpleNamespace(_monitor=monitor)
    entry = await _entry(worker_app.refund_queue, "TXR6", rail="TON", nonce="abc:sid-test")
    assert await refund_worker._recover_payment_info(worker_app, entry) is False


# ── _tick: drains due entries, swallows per-entry errors ───────────────


async def test_tick_processes_each_due_entry(worker_app, patch_onchain):
    await _entry(worker_app.refund_queue, "D1", sender="EQu", amount=1000)
    await _entry(worker_app.refund_queue, "D2", sender="EQu", amount=1000)

    await _tick(worker_app)

    assert (await worker_app.refund_queue.get("D1")).status == "refunded"
    assert (await worker_app.refund_queue.get("D2")).status == "refunded"


async def test_tick_swallows_per_entry_errors(worker_app, monkeypatch):
    await _entry(worker_app.refund_queue, "D3", sender="EQu", amount=1000)
    monkeypatch.setattr(refund_worker, "_process_entry",
                        AsyncMock(side_effect=RuntimeError("boom")))
    # Must not propagate — one bad entry can't kill the tick.
    await _tick(worker_app)


# ── _recover_stale_refunding: crash-recovery dispatch ──────────────────


async def test_stale_recovery_adopts_onchain_refund(worker_app, patch_onchain, monkeypatch):
    monkeypatch.setattr(refund_worker, "_acquire_lite_client",
                        lambda app: _noop_client())
    patch_onchain.find_existing.return_value = "FOUND_REFUND"
    await worker_app.refund_queue.enqueue(tx_hash="S1", nonce="n", rail="TON",
                                          sender="EQu", amount=1000)
    await worker_app.refund_queue.claim("S1")  # → refunding

    await _recover_stale_refunding(worker_app, older_than_seconds=-1)

    rec = await worker_app.refund_queue.get("S1")
    assert rec.status == "refunded" and rec.refund_tx == "FOUND_REFUND"


async def test_stale_recovery_reverts_to_pending_when_no_onchain_refund(worker_app, patch_onchain, monkeypatch):
    monkeypatch.setattr(refund_worker, "_acquire_lite_client",
                        lambda app: _noop_client())
    patch_onchain.find_existing.return_value = None
    await worker_app.refund_queue.enqueue(tx_hash="S2", nonce="n", rail="USDT",
                                          sender="EQu", amount=1000)
    await worker_app.refund_queue.claim("S2")

    await _recover_stale_refunding(worker_app, older_than_seconds=-1)

    assert (await worker_app.refund_queue.get("S2")).status == "pending"
