"""Tests for transfer.py — body builders and TransferSender retry logic."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chains.ton.transfer as transfer_module
from chains.ton.transfer import (
    HEARTBEAT_OPCODE,
    PAYMENT_OPCODE,
    REFUND_OPCODE,
    TransferSender,
    heartbeat_body,
    payment_body,
    refund_body,
    text_comment_body,
)


# ── Body builders ──────────────────────────────────────────────────────

def _parse_cell(cell) -> tuple[int, str]:
    """Return (opcode, snake-string-payload) from a cell built above."""
    slice_ = cell.begin_parse()
    opcode = slice_.load_uint(32)
    payload = slice_.load_snake_string()
    return opcode, payload


def test_heartbeat_body_encoding():
    cell = heartbeat_body("hello world")
    op, payload = _parse_cell(cell)
    assert op == HEARTBEAT_OPCODE
    assert payload == "hello world"


def test_payment_body_encoding():
    cell = payment_body("abcdef:xyz")
    op, payload = _parse_cell(cell)
    assert op == PAYMENT_OPCODE
    assert payload == "abcdef:xyz"


def test_text_comment_body_uses_opcode_zero():
    cell = text_comment_body("plain comment")
    op, payload = _parse_cell(cell)
    assert op == 0
    assert payload == "plain comment"


def test_refund_body_carries_json_payload():
    cell = refund_body("txhash123", "timeout", "sidecar-id-1")
    op, payload = _parse_cell(cell)
    assert op == REFUND_OPCODE
    decoded = json.loads(payload)
    assert decoded == {"tx": "txhash123", "reason": "timeout", "sidecar_id": "sidecar-id-1"}


def test_payment_body_roundtrips_unicode():
    cell = payment_body("нонс:сайдкар")
    _, payload = _parse_cell(cell)
    assert payload == "нонс:сайдкар"


def test_opcodes_are_distinct():
    assert HEARTBEAT_OPCODE != PAYMENT_OPCODE != REFUND_OPCODE
    assert HEARTBEAT_OPCODE != 0 and PAYMENT_OPCODE != 0 and REFUND_OPCODE != 0


# ── TransferSender ──────────────────────────────────────────────────────

@pytest.fixture
def sender() -> TransferSender:
    return TransferSender(private_key_hex="a" * 64, testnet=True)


@pytest.fixture(autouse=True)
def _submit_via_wallet_mock(request, monkeypatch):
    """Retry-logic tests below drive `send()` through a mocked `wallet.transfer`;
    route `_submit` through it. `_submit` itself is tested separately."""
    if request.node.get_closest_marker("real_submit"):
        return

    async def fake_submit(self, destination, amount, body):
        msg = await self._wallet.transfer(destination=destination, amount=amount, body=body, bounce=False)
        return msg.normalized_hash

    monkeypatch.setattr(TransferSender, "_submit", fake_submit)


async def test_sender_send_success_first_attempt(sender, monkeypatch):
    wallet = MagicMock()
    msg = MagicMock()
    msg.normalized_hash = "HASH_OK"
    wallet.transfer = AsyncMock(return_value=msg)

    async def fake_init(self):
        self._client = MagicMock()
        self._wallet = wallet

    async def fake_find(self, target_hashes):
        return "HASH_OK" if "HASH_OK" in target_hashes else None

    monkeypatch.setattr(TransferSender, "_ensure_initialized", fake_init)
    monkeypatch.setattr(TransferSender, "_find_landed_hash", fake_find)
    monkeypatch.setattr(transfer_module, "CONFIRM_POLL_INTERVAL_SEC", 0)

    result = await sender.send("EQdestination", 1_000, MagicMock())
    assert result == "HASH_OK"
    wallet.transfer.assert_awaited_once()


async def test_sender_send_retries_then_succeeds(sender, monkeypatch):
    wallet = MagicMock()
    attempts = {"n": 0}

    async def flaky_transfer(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionError("liteserver down")
        msg = MagicMock()
        msg.normalized_hash = "HASH_RETRY"
        return msg

    wallet.transfer = flaky_transfer

    async def fake_init(self):
        self._client = MagicMock()
        self._client.close = AsyncMock()
        self._wallet = wallet

    async def fake_reconnect(self):
        await fake_init(self)

    async def fake_find(self, target_hashes):
        return "HASH_RETRY" if "HASH_RETRY" in target_hashes else None

    monkeypatch.setattr(TransferSender, "_ensure_initialized", fake_init)
    monkeypatch.setattr(TransferSender, "_reconnect", fake_reconnect)
    monkeypatch.setattr(TransferSender, "_find_landed_hash", fake_find)
    # Speed up retry delays
    monkeypatch.setattr(transfer_module, "SEND_RETRY_DELAYS", [0, 0, 0])
    monkeypatch.setattr(transfer_module, "CONFIRM_POLL_INTERVAL_SEC", 0)

    result = await sender.send("EQdest", 1_000, MagicMock())
    assert result == "HASH_RETRY"
    assert attempts["n"] == 2


async def test_sender_send_raises_after_max_retries(sender, monkeypatch):
    wallet = MagicMock()
    wallet.transfer = AsyncMock(side_effect=ConnectionError("permanently broken"))

    async def fake_init(self):
        self._client = MagicMock()
        self._client.close = AsyncMock()
        self._wallet = wallet

    async def fake_find(self, target_hashes):
        return None  # nothing ever lands

    monkeypatch.setattr(TransferSender, "_ensure_initialized", fake_init)
    monkeypatch.setattr(TransferSender, "_reconnect", fake_init)
    monkeypatch.setattr(TransferSender, "_find_landed_hash", fake_find)
    monkeypatch.setattr(transfer_module, "SEND_RETRY_DELAYS", [0, 0, 0])
    monkeypatch.setattr(transfer_module, "SEND_MAX_RETRIES", 3)
    monkeypatch.setattr(transfer_module, "CONFIRM_POLL_INTERVAL_SEC", 0)

    with pytest.raises(ConnectionError, match="permanently broken"):
        await sender.send("EQdest", 1_000, MagicMock())
    assert wallet.transfer.await_count == 3


async def test_sender_send_is_serialized_by_lock(sender, monkeypatch):
    """Two concurrent send() calls must not overlap — wallet access is serialized."""
    active = {"count": 0, "max": 0}

    wallet = MagicMock()

    async def tracked_transfer(**kwargs):
        active["count"] += 1
        active["max"] = max(active["max"], active["count"])
        await asyncio.sleep(0.02)
        active["count"] -= 1
        m = MagicMock()
        m.normalized_hash = "HASH"
        return m

    wallet.transfer = tracked_transfer

    async def fake_init(self):
        self._client = MagicMock()
        self._wallet = wallet

    async def fake_find(self, target_hashes):
        # Always confirm whatever was just submitted
        return next(iter(target_hashes)) if target_hashes else None

    monkeypatch.setattr(TransferSender, "_ensure_initialized", fake_init)
    monkeypatch.setattr(TransferSender, "_find_landed_hash", fake_find)
    monkeypatch.setattr(transfer_module, "CONFIRM_POLL_INTERVAL_SEC", 0)

    await asyncio.gather(
        sender.send("EQa", 1, MagicMock()),
        sender.send("EQb", 1, MagicMock()),
        sender.send("EQc", 1, MagicMock()),
    )
    assert active["max"] == 1


async def test_sender_close_uninitialized_is_noop(sender):
    # Must not raise if close() is called before any send.
    await sender.close()
    assert sender._client is None


async def test_sender_close_cleans_client(sender):
    sender._client = MagicMock()
    sender._client.close = AsyncMock()
    sender._wallet = MagicMock()
    await sender.close()
    assert sender._client is None
    assert sender._wallet is None


# ── _submit: HTTP broadcast first, liteserver last ──────────────────────

def _submit_sender(broadcaster) -> TransferSender:
    s = TransferSender(private_key_hex="a" * 64, testnet=True, broadcaster=broadcaster)
    ext = MagicMock()
    ext.as_hex = "b5ee"
    ext.normalized_hash = "HASH_EXT"
    s._wallet = MagicMock()
    s._wallet.build_external_message = AsyncMock(return_value=ext)
    s._client = MagicMock()
    s._client.send_message = AsyncMock()
    return s


@pytest.mark.real_submit
async def test_submit_broadcasts_over_http_not_liteserver():
    broadcaster = MagicMock()
    broadcaster.send_boc = AsyncMock(return_value="toncenter")
    s = _submit_sender(broadcaster)

    assert await s._submit("EQdest", 1_000, MagicMock()) == "HASH_EXT"
    broadcaster.send_boc.assert_awaited_once_with(bytes.fromhex("b5ee"))
    s._client.send_message.assert_not_awaited()


@pytest.mark.real_submit
async def test_submit_falls_back_to_liteserver_when_http_fails():
    broadcaster = MagicMock()
    broadcaster.send_boc = AsyncMock(side_effect=RuntimeError("both down"))
    s = _submit_sender(broadcaster)

    assert await s._submit("EQdest", 1_000, MagicMock()) == "HASH_EXT"
    s._client.send_message.assert_awaited_once_with("b5ee")


@pytest.mark.real_submit
async def test_submit_sets_finite_valid_until(monkeypatch):
    """seqno == 0 must not get tonutils' 0xFFFFFFFF — retries would be identical."""
    broadcaster = MagicMock()
    broadcaster.send_boc = AsyncMock(return_value="toncenter")
    s = _submit_sender(broadcaster)
    monkeypatch.setattr(transfer_module.time, "time", lambda: 1_000_000)

    await s._submit("EQdest", 1_000, MagicMock())
    params = s._wallet.build_external_message.await_args.args[1]
    assert params.valid_until == 1_000_000 + transfer_module.MESSAGE_TTL_SEC
