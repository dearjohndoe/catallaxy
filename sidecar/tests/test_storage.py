"""Tests for storage.py — StateStore and SidecarState."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from storage import SidecarState, StateStore


def test_load_missing_file_returns_empty_state(tmp_path: Path):
    store = StateStore(str(tmp_path / "missing.json"))
    state = store.load()
    assert isinstance(state, SidecarState)
    assert state.last_heartbeat is None
    assert state.sidecar_id is None


def test_load_corrupted_json_raises(tmp_state_path: str):
    # Losing sidecar_id silently would re-register the agent under a fresh
    # identity on the registry and strand in-flight payments. The contract is
    # "crash loudly" so the operator notices and restores from backup.
    Path(tmp_state_path).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        StateStore(tmp_state_path).load()


def test_load_partial_payload_raises(tmp_state_path: str):
    # Same rationale: a partial state file (e.g. one written by an older
    # version that only knew about last_heartbeat) must not silently default
    # sidecar_id to None.
    Path(tmp_state_path).write_text(json.dumps({"last_heartbeat": "2024-01-01T00:00:00Z"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing required keys"):
        StateStore(tmp_state_path).load()


def test_save_then_load_roundtrip(tmp_state_path: str):
    store = StateStore(tmp_state_path)
    original = SidecarState(
        last_heartbeat="2024-06-01T12:34:56Z",
        sidecar_id="abc-123",
    )
    store.save(original)
    loaded = store.load()
    assert loaded == original


def test_save_overwrites_previous_state(tmp_state_path: str):
    store = StateStore(tmp_state_path)
    store.save(SidecarState(last_heartbeat="A", sidecar_id="id1"))
    store.save(SidecarState(last_heartbeat="B", sidecar_id="id2"))
    assert store.load() == SidecarState(last_heartbeat="B", sidecar_id="id2")


def test_save_writes_utf8_and_non_ascii(tmp_state_path: str):
    store = StateStore(tmp_state_path)
    store.save(SidecarState(last_heartbeat="тест", sidecar_id="émoji-🚀"))
    loaded = store.load()
    assert loaded.last_heartbeat == "тест"
    assert loaded.sidecar_id == "émoji-🚀"


def test_save_produces_pretty_indented_json(tmp_state_path: str):
    store = StateStore(tmp_state_path)
    store.save(SidecarState(last_heartbeat="t", sidecar_id="i"))
    raw = Path(tmp_state_path).read_text(encoding="utf-8")
    # indent=2 → contains newlines and 2-space indent
    assert "\n" in raw
    assert '  "' in raw


def test_save_both_fields_none(tmp_state_path: str):
    store = StateStore(tmp_state_path)
    store.save(SidecarState())
    data = json.loads(Path(tmp_state_path).read_text(encoding="utf-8"))
    assert data == {"last_heartbeat": None, "sidecar_id": None, "last_sol_heartbeat": None}


def test_load_empty_file_raises(tmp_state_path: str):
    # An empty state file is not a valid identity — crash loudly.
    Path(tmp_state_path).write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        StateStore(tmp_state_path).load()


def test_load_non_object_json_raises(tmp_state_path: str):
    # A JSON list/scalar is not a valid identity envelope. Must crash rather
    # than silently reset sidecar_id (which would strand in-flight payments).
    Path(tmp_state_path).write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected a JSON object"):
        StateStore(tmp_state_path).load()


def test_load_scalar_json_raises(tmp_state_path: str):
    # Same rationale — "42" parses as JSON but is not an identity envelope.
    Path(tmp_state_path).write_text("42", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected a JSON object"):
        StateStore(tmp_state_path).load()
