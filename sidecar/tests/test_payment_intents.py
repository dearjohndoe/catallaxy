"""payment_intents: same-txn mark, status flips, stale recovery → force_refund."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiosqlite
import pytest

from api.domain.refund_worker import recover_stale_intents
from payments.processed_tx import (
    INTENT_ACCEPTED,
    INTENT_FULFILLED,
    INTENT_REFUNDED,
    PaymentIntentDraft,
    ProcessedTxStore,
)
from payments.refund_queue import RefundQueue


def _draft(**overrides) -> PaymentIntentDraft:
    base = dict(
        identity="ton:pub:aabbccdd",
        nonce="aabbccdd:sid-test",
        rail="TON",
        sender="EQsender",
        amount=1_000_000,
        sku_id="default",
    )
    base.update(overrides)
    return PaymentIntentDraft(**base)


@pytest.fixture
async def store(tmp_path):
    s = ProcessedTxStore(str(tmp_path / "ptx.db"))
    await s.init()
    yield s
    await s.close()


async def test_mark_with_intent_commits_hash_and_accepted_row(store):
    await store.mark_processed("ton:h1", intent=_draft())
    assert await store.is_processed("ton:h1") is True
    row = await store.get_intent("ton:h1")
    assert row is not None
    assert row.status == INTENT_ACCEPTED
    assert row.identity == "ton:pub:aabbccdd"
    assert row.sender == "EQsender"
    assert row.amount == 1_000_000
    assert row.rail == "TON"


async def test_mark_without_intent_does_not_create_intent(store):
    await store.mark_processed("ton:h2")
    assert await store.is_processed("ton:h2") is True
    assert await store.get_intent("ton:h2") is None


async def test_mark_rolls_back_hash_if_intent_insert_fails(store):
    orig = store._conn.execute

    async def boom(sql, parameters=None):
        if isinstance(sql, str) and "INSERT INTO payment_intents" in sql:
            raise aiosqlite.OperationalError("simulated intent insert fail")
        if parameters is None:
            return await orig(sql)
        return await orig(sql, parameters)

    store._conn.execute = boom  # type: ignore[method-assign]
    with pytest.raises(aiosqlite.OperationalError):
        await store.mark_processed("ton:h3", intent=_draft())
    store._conn.execute = orig  # type: ignore[method-assign]
    assert await store.is_processed("ton:h3") is False
    assert await store.get_intent("ton:h3") is None


async def test_set_intent_status_only_from_accepted(store):
    await store.mark_processed("ton:h4", intent=_draft())
    assert await store.set_intent_status("ton:h4", INTENT_FULFILLED) is True
    assert (await store.get_intent("ton:h4")).status == INTENT_FULFILLED
    assert await store.set_intent_status("ton:h4", INTENT_REFUNDED) is False
    assert (await store.get_intent("ton:h4")).status == INTENT_FULFILLED


async def test_list_stale_accepted_age_filter(store):
    await store.mark_processed("ton:h5", intent=_draft())
    assert await store.list_stale_accepted(older_than_seconds=10_000) == []
    stale = await store.list_stale_accepted(older_than_seconds=0)
    assert len(stale) == 1 and stale[0].tx_hash == "ton:h5"


async def test_cleanup_keeps_accepted_deletes_old_terminal(store):
    await store.mark_processed("ton:keep", intent=_draft(identity="ton:pub:11111111"))
    await store.mark_processed("ton:drop", intent=_draft(identity="ton:pub:22222222"))
    assert await store.set_intent_status("ton:drop", INTENT_FULFILLED) is True
    old = int(time.time()) - 40 * 24 * 3600
    await store._conn.execute(
        "UPDATE payment_intents SET created_at = ? WHERE tx_hash = ?",
        (old, "ton:drop"),
    )
    await store._conn.commit()
    await store.cleanup(older_than_seconds=30 * 24 * 3600)
    assert await store.get_intent("ton:keep") is not None
    assert await store.get_intent("ton:drop") is None


async def test_recover_stale_intents_enqueues_force_refund(tmp_path):
    db = str(tmp_path / "same.db")
    txs = ProcessedTxStore(db)
    rq = RefundQueue(db)
    await txs.init()
    await rq.init()
    try:
        await txs.mark_processed("ton:crash", intent=_draft())
        app = SimpleNamespace(
            tx_store=txs,
            refund_queue=rq,
            owner_bot=None,
        )
        await recover_stale_intents(app, older_than_seconds=0)
        entry = await rq.get("ton:pub:aabbccdd")
        assert entry is not None
        assert entry.force_refund == 1
        assert entry.sender == "EQsender"
        assert entry.amount == 1_000_000
        assert (await txs.get_intent("ton:crash")).status == INTENT_REFUNDED

        await recover_stale_intents(app, older_than_seconds=0)
        assert (await rq.get("ton:pub:aabbccdd")).status == "pending"
    finally:
        await rq.close()
        await txs.close()


async def test_recover_skips_fulfilled(tmp_path):
    db = str(tmp_path / "same.db")
    txs = ProcessedTxStore(db)
    rq = RefundQueue(db)
    await txs.init()
    await rq.init()
    try:
        await txs.mark_processed("ton:ok", intent=_draft())
        await txs.set_intent_status("ton:ok", INTENT_FULFILLED)
        app = SimpleNamespace(tx_store=txs, refund_queue=rq, owner_bot=None)
        await recover_stale_intents(app, older_than_seconds=0)
        assert await rq.get("ton:pub:aabbccdd") is None
    finally:
        await rq.close()
        await txs.close()


async def test_recover_notifies_owner_bot(tmp_path):
    db = str(tmp_path / "same.db")
    txs = ProcessedTxStore(db)
    rq = RefundQueue(db)
    await txs.init()
    await rq.init()
    bot = MagicMock()
    try:
        await txs.mark_processed("ton:crash", intent=_draft())
        app = SimpleNamespace(tx_store=txs, refund_queue=rq, owner_bot=bot)
        await recover_stale_intents(app, older_than_seconds=0)
        bot.notify_refund.assert_called_once()
        assert bot.notify_refund.call_args.kwargs["reason"] == "stale_payment_intent"
    finally:
        await rq.close()
        await txs.close()
