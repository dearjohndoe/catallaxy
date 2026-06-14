"""Tests for heartbeat.py — payload building, scheduling, send_if_needed, loop."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from heartbeat import HeartbeatConfig, HeartbeatManager
from storage import SidecarState, StateStore
from transfer import HEARTBEAT_OPCODE


def _make_config(**overrides) -> HeartbeatConfig:
    base = dict(
        registry_address="EQregistry",
        endpoint="https://agent.test",
        price=1_000_000,
        capabilities=("translate",),
        name="Translator",
        description="Translates text",
        args_schema={"text": {"type": "string", "required": True}},
        has_quote=False,
        sidecar_id="sid-abc",
        result_schema=None,
    )
    base.update(overrides)
    return HeartbeatConfig(**base)


def _make_manager(tmp_state_path: str, sender: AsyncMock | None = None, **cfg):
    store = StateStore(tmp_state_path)
    if sender is None:
        sender = AsyncMock(return_value="HASH")
    manager = HeartbeatManager(
        config=_make_config(**cfg),
        state_store=store,
        transfer_sender=sender,
    )
    return manager, store, sender


# ── _build_payload ─────────────────────────────────────────────────────

def test_build_payload_minimal(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path, sidecar_id=None, has_quote=False, result_schema=None)
    payload = manager._build_payload()
    assert payload["name"] == "Translator"
    assert payload["capabilities"] == ["translate"]
    assert payload["price"] == 1_000_000
    assert payload["endpoint"] == "https://agent.test"
    assert payload["args_schema"] == {"text": {"type": "string", "required": True}}
    # Optional fields must be absent
    assert "has_quote" not in payload
    assert "sidecar_id" not in payload
    assert "result_schema" not in payload


def test_build_payload_multiple_capabilities(tmp_state_path):
    manager, _, _ = _make_manager(
        tmp_state_path, capabilities=("games.topup", "games"),
        sidecar_id=None, has_quote=False, result_schema=None,
    )
    payload = manager._build_payload()
    assert payload["capabilities"] == ["games.topup", "games"]


def test_build_payload_with_quote_and_id(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path, has_quote=True, sidecar_id="sidecar-42")
    payload = manager._build_payload()
    assert payload["has_quote"] is True
    assert payload["sidecar_id"] == "sidecar-42"


def test_build_payload_with_result_schema(tmp_state_path):
    schema = {"type": "object", "properties": {"output": {"type": "string"}}}
    manager, _, _ = _make_manager(tmp_state_path, result_schema=schema)
    payload = manager._build_payload()
    assert payload["result_schema"] == schema


# ── _should_send_now ───────────────────────────────────────────────────

def test_should_send_now_when_no_state(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    assert manager._should_send_now(SidecarState(last_heartbeat=None)) is True


def test_should_send_now_when_state_corrupted(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    assert manager._should_send_now(SidecarState(last_heartbeat="garbage")) is True


def test_should_send_now_when_recent(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    assert manager._should_send_now(SidecarState(last_heartbeat=recent)) is False


def test_should_send_now_when_older_than_threshold(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    stale = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    assert manager._should_send_now(SidecarState(last_heartbeat=stale)) is True


def test_should_send_now_handles_plus_offset(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    recent = datetime.now(timezone.utc).isoformat()  # contains "+00:00"
    assert manager._should_send_now(SidecarState(last_heartbeat=recent)) is False


# ── send_if_needed ─────────────────────────────────────────────────────

async def test_send_if_needed_first_time_sends(tmp_state_path):
    manager, store, sender = _make_manager(tmp_state_path)
    sent = await manager.send_if_needed()
    assert sent is True
    sender.assert_awaited_once()
    dest, amount, body = sender.call_args.args
    assert dest == "EQregistry"
    assert amount == 10_000_000
    # Body is a Cell — decode comment to check the payload.
    slice_ = body.begin_parse()
    assert slice_.load_uint(32) == HEARTBEAT_OPCODE
    decoded = json.loads(slice_.load_snake_string())
    assert decoded["name"] == "Translator"
    # State persists the timestamp.
    state = store.load()
    assert state.last_heartbeat is not None
    assert state.last_heartbeat.endswith("Z")


async def test_send_if_needed_skipped_when_recent(tmp_state_path):
    manager, store, sender = _make_manager(tmp_state_path)
    recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.save(SidecarState(last_heartbeat=recent))
    sent = await manager.send_if_needed()
    assert sent is False
    sender.assert_not_awaited()


async def test_send_if_needed_force_true_always_sends(tmp_state_path):
    manager, store, sender = _make_manager(tmp_state_path)
    recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store.save(SidecarState(last_heartbeat=recent))
    sent = await manager.send_if_needed(force=True)
    assert sent is True
    sender.assert_awaited_once()


async def test_send_if_needed_transfer_failure_propagates(tmp_state_path):
    sender = AsyncMock(side_effect=RuntimeError("ton down"))
    manager, store, _ = _make_manager(tmp_state_path, sender=sender)
    with pytest.raises(RuntimeError, match="ton down"):
        await manager.send_if_needed()
    # Critical: state must NOT be updated if the transfer failed, so we'll retry.
    assert store.load().last_heartbeat is None


async def test_send_if_needed_body_contains_all_payload_fields(tmp_state_path):
    manager, _, sender = _make_manager(
        tmp_state_path,
        has_quote=True,
        sidecar_id="sid-xyz",
        result_schema={"type": "object"},
    )
    await manager.send_if_needed()
    _, _, body = sender.call_args.args
    slice_ = body.begin_parse()
    slice_.load_uint(32)
    decoded = json.loads(slice_.load_snake_string())
    assert decoded["has_quote"] is True
    assert decoded["sidecar_id"] == "sid-xyz"
    assert decoded["result_schema"] == {"type": "object"}


# ── loop ───────────────────────────────────────────────────────────────

async def test_loop_exits_on_stop_event(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    stop = asyncio.Event()

    # Stub out send_if_needed so the loop ticks cheaply.
    manager.send_if_needed = AsyncMock(return_value=False)

    task = asyncio.create_task(manager.loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert task.done()
    assert manager.send_if_needed.await_count >= 1


async def test_loop_swallows_exceptions(tmp_state_path):
    manager, _, _ = _make_manager(tmp_state_path)
    stop = asyncio.Event()
    calls = {"n": 0}

    async def flaky(force: bool = False):
        calls["n"] += 1
        raise RuntimeError("flake")

    manager.send_if_needed = flaky  # type: ignore[assignment]

    task = asyncio.create_task(manager.loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    # It ran at least once and didn't crash the loop.
    assert calls["n"] >= 1
