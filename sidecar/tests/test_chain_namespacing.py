"""Chain-namespaced tx keys: helper semantics + the legacy-key migrations that
keep ``{chain}:{tx_hash}`` keying correct in processed_txs / refund_queue."""

from __future__ import annotations

import aiosqlite
import pytest

from chains.base import chain_for_rail, namespaced_pub_key, namespaced_tx_key
from payments.nonce import pub_from_nonce
from payments.processed_tx import ProcessedTxStore
from payments.refund_queue import RefundQueue


# ── helpers ────────────────────────────────────────────────────────────


def test_chain_for_rail_maps_ton_rails_to_ton():
    assert chain_for_rail("TON") == "ton"
    assert chain_for_rail("USDT") == "ton"


def test_chain_for_rail_unknown_falls_back_to_lowercase():
    assert chain_for_rail("SOL") == "sol"


def test_namespaced_tx_key_prefixes_bare_hash():
    assert namespaced_tx_key("ton", "abc123") == "ton:abc123"


def test_namespaced_tx_key_is_idempotent_on_already_namespaced():
    # A value already containing ':' is returned unchanged (safe double-wrap).
    assert namespaced_tx_key("ton", "ton:abc123") == "ton:abc123"
    assert namespaced_tx_key("sol", "ton:abc123") == "ton:abc123"


def test_namespaced_pub_key_distinct_from_tx_key():
    assert namespaced_pub_key("ton", "aabbccdd") == "ton:pub:aabbccdd"
    # An 8-hex client tx must not collide with the pub identity.
    assert namespaced_tx_key("ton", "aabbccdd") != namespaced_pub_key("ton", "aabbccdd")


def test_namespaced_pub_key_is_idempotent():
    assert namespaced_pub_key("ton", "ton:pub:aabbccdd") == "ton:pub:aabbccdd"
    assert namespaced_pub_key("sol", "ton:pub:aabbccdd") == "ton:pub:aabbccdd"


def test_pub_from_nonce_full_and_pub_nonce():
    assert pub_from_nonce("aabbccdd11223344:sid") == "aabbccdd"
    assert pub_from_nonce("aabbccdd:sid") == "aabbccdd"
    assert pub_from_nonce("n") is None
    assert pub_from_nonce("short:sid") is None
    assert pub_from_nonce("") is None


# ── ProcessedTxStore migration ─────────────────────────────────────────


async def test_processed_tx_migration_prefixes_legacy_bare_keys(tmp_path):
    db = str(tmp_path / "ptx.db")
    # Seed a legacy DB with bare keys, as written before multichain.
    conn = await aiosqlite.connect(db)
    await conn.execute(
        "CREATE TABLE processed_txs (tx_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
    )
    await conn.execute("INSERT INTO processed_txs VALUES ('barehash', '2026-01-01')")
    await conn.commit()
    await conn.close()

    store = ProcessedTxStore(db)
    await store.init()
    try:
        # Legacy key is now reachable only under its namespaced form.
        assert await store.is_processed("ton:barehash") is True
        assert await store.is_processed("barehash") is False
    finally:
        await store.close()


async def test_processed_tx_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "ptx.db")
    store = ProcessedTxStore(db)
    await store.init()
    await store.mark_processed("ton:already")  # already namespaced
    await store.close()

    # Re-open: init() re-runs the migration; must not double-prefix.
    store2 = ProcessedTxStore(db)
    await store2.init()
    try:
        assert await store2.is_processed("ton:already") is True
        assert await store2.is_processed("ton:ton:already") is False
    finally:
        await store2.close()


# ── RefundQueue migration ──────────────────────────────────────────────


async def test_refund_queue_migration_prefixes_legacy_bare_keys(tmp_path):
    db = str(tmp_path / "rq.db")
    # Seed via a first init (creates schema), bare key inserted directly.
    rq = RefundQueue(db)
    await rq.init()
    await rq._conn.execute(
        "INSERT INTO pending_refunds (tx_hash, nonce, rail, status, attempts, "
        "created_at, next_attempt_at) VALUES ('barehash', 'n', 'TON', 'pending', 0, 1, 1)"
    )
    await rq._conn.commit()
    await rq.close()

    # Re-open: init() runs the migration over the legacy bare row.
    rq2 = RefundQueue(db)
    await rq2.init()
    try:
        assert await rq2.get("ton:barehash") is not None
        assert await rq2.get("barehash") is None
    finally:
        await rq2.close()
