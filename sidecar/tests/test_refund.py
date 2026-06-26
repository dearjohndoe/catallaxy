"""Characterization tests for api.domain.refund.

Locks the per-rail refund dispatch (``refund_user``: TON ``send`` vs USDT
``send_jetton`` with their distinct fee math), the direct-then-enqueue fallback
(``refund_or_enqueue``), and the on-chain dedup scan (``find_existing_refund_tx``)
before the multichain refactor turns ``rail == "USDT"`` branching into
ChainRail-object dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chains.ton.transfer import refund_body
from api.domain.refund import find_existing_refund_tx, refund_or_enqueue
from payments.refund_queue import RefundQueue

# NOTE: per-rail refund sending (the old ``refund_user``) moved to the ChainRail
# adapters; its characterization now lives in tests/test_rails_ton.py. What
# stays here is the rail-agnostic orchestration.


# ── refund_or_enqueue ──────────────────────────────────────────────────


@pytest.fixture
async def queue(tmp_path):
    rq = RefundQueue(str(tmp_path / "rq.db"))
    await rq.init()
    yield rq
    await rq.close()


async def test_refund_or_enqueue_returns_tx_on_direct_success(queue):
    refund_fn = AsyncMock(return_value="DIRECT_TX")
    out = await refund_or_enqueue(
        refund_queue=queue, refund_user_fn=refund_fn, tx_hash="TX", nonce="n",
        rail="TON", sender="EQu", amount=1000, sku_id="sku", reason="oos",
    )
    assert out == "DIRECT_TX"
    assert await queue.get("TX") is None  # nothing enqueued


async def test_refund_or_enqueue_enqueues_force_refund_when_direct_returns_none(queue):
    refund_fn = AsyncMock(return_value=None)
    out = await refund_or_enqueue(
        refund_queue=queue, refund_user_fn=refund_fn, tx_hash="TX", nonce="n",
        rail="USDT", sender="EQu", amount=1000, sku_id="sku", reason="oos",
    )
    assert out is None
    rec = await queue.get("TX")
    assert rec is not None and rec.force_refund == 1 and rec.rail == "USDT"


async def test_refund_or_enqueue_enqueues_when_direct_raises(queue):
    refund_fn = AsyncMock(side_effect=RuntimeError("boom"))
    out = await refund_or_enqueue(
        refund_queue=queue, refund_user_fn=refund_fn, tx_hash="TX2", nonce="n",
        rail="TON", sender="EQu", amount=1000, sku_id="sku", reason="oos",
    )
    assert out is None
    rec = await queue.get("TX2")
    assert rec is not None and rec.force_refund == 1


# ── find_existing_refund_tx ────────────────────────────────────────────


async def test_find_existing_returns_none_when_get_transactions_raises():
    client = SimpleNamespace(get_transactions=AsyncMock(side_effect=RuntimeError("rpc")))
    out = await find_existing_refund_tx(
        client=client, agent_wallet="EQagent", rail="TON",
        original_tx_hash="tx", sidecar_id="sid",
    )
    assert out is None


async def test_find_existing_returns_none_when_no_match():
    tx = SimpleNamespace(out_msgs=[SimpleNamespace(body=None)],
                         cell=SimpleNamespace(hash=b"\xaa"))
    client = SimpleNamespace(get_transactions=AsyncMock(return_value=[tx]))
    out = await find_existing_refund_tx(
        client=client, agent_wallet="EQagent", rail="USDT",
        original_tx_hash="tx", sidecar_id="sid",
    )
    assert out is None


async def test_find_existing_matches_ton_refund_by_fingerprint():
    body = refund_body("orig-tx", "timeout", "sid")
    tx = SimpleNamespace(out_msgs=[SimpleNamespace(body=body)],
                         cell=SimpleNamespace(hash=b"\xab\xcd"))
    client = SimpleNamespace(get_transactions=AsyncMock(return_value=[tx]))
    out = await find_existing_refund_tx(
        client=client, agent_wallet="EQagent", rail="TON",
        original_tx_hash="orig-tx", sidecar_id="sid",
    )
    assert out == "abcd"


async def test_find_existing_ignores_ton_refund_with_different_fingerprint():
    body = refund_body("some-other-tx", "timeout", "sid")
    tx = SimpleNamespace(out_msgs=[SimpleNamespace(body=body)],
                         cell=SimpleNamespace(hash=b"\xab\xcd"))
    client = SimpleNamespace(get_transactions=AsyncMock(return_value=[tx]))
    out = await find_existing_refund_tx(
        client=client, agent_wallet="EQagent", rail="TON",
        original_tx_hash="orig-tx", sidecar_id="sid",
    )
    assert out is None
