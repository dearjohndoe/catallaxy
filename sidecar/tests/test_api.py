"""Tests for api.py — SidecarApp handlers, middleware, validators, helpers."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import api as api_module
from api import QuoteEntry, SidecarApp, fetch_describe, validate_body
from settings import AgentSku, DEFAULT_SKU_ID, Settings
from payments import PaymentVerificationError, VerifiedPayment


# ── Settings factory ───────────────────────────────────────────────────

def make_settings(tmp_path: Path, **overrides) -> Settings:
    agent_price = overrides.get("agent_price", 1_000_000)
    agent_price_usdt = overrides.get("agent_price_usdt", None)
    default_sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID,
        price_ton=agent_price if agent_price else None,
        price_usd=agent_price_usdt,
        initial_stock=None,
    )
    rails: list[str] = []
    if default_sku.price_ton is not None:
        rails.append("TON")
    if default_sku.price_usd is not None:
        rails.append("USDT")
    base = dict(
        agent_command="true",
        capability="translate",
        agent_name="Translator",
        agent_description="Translates text",
        agent_price=agent_price,
        agent_endpoint="https://agent.test",
        agent_wallet_pk="a" * 64,
        agent_wallet_seed=None,
        agent_wallet="EQagent",
        registry_address="EQregistry",
        port=8080,
        payment_timeout=300,
        sync_timeout=30,
        final_timeout=1200,
        jobs_ttl=3600,
        testnet=True,
        state_path=str(tmp_path / "state.json"),
        tx_db_path=str(tmp_path / "tx.db"),
        stock_db_path=str(tmp_path / "stock.db"),
        enforce_comment_nonce=True,
        refund_fee_nanoton=500_000,
        refund_worker_interval=60,
        refund_max_attempts=10,
        agent_price_usdt=agent_price_usdt,
        has_quote=False,
        rate_limit_requests=3,
        rate_limit_window=10,
        trusted_proxy_ips=frozenset(),
        file_store_dir=str(tmp_path / "file_store"),
        file_store_ttl=60,
        images_dir=str(tmp_path / "images"),
        agent_preview_url=None,
        agent_avatar_url=None,
        agent_images=(),
        owner_wallet=None,
        skus=(default_sku,),
        payment_rails=tuple(rails),
        tg_bot_token=None,
        tg_user_ids=(),
    )
    base.update(overrides)
    return Settings(**base)


async def _close_app_stores(app: SidecarApp) -> None:
    """Close every SQLite-backed store the invoke flow may have lazily opened.

    ProcessedTxStore / RefundQueue auto-``init()`` on first use, so a test that
    exercises the invoke/refund path opens connections the noop shutdown never
    knew about. Each ``close()`` is idempotent (guards on ``_conn``), so closing
    all three unconditionally is safe even when a store was never touched.
    """
    await app.stock.close()
    await app.tx_store.close()
    await app.refund_queue.close()


@pytest.fixture
def app_factory(tmp_path):
    def _make(**overrides) -> SidecarApp:
        settings = make_settings(tmp_path, **overrides)
        app = SidecarApp(settings)
        app.sidecar_id = "sid-test"
        app.args_schema = {
            "text": {"type": "string", "required": True},
            "lang": {"type": "string", "required": False},
        }
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        return app
    return _make


# ── validate_body ──────────────────────────────────────────────────────

def test_validate_body_all_required_present():
    schema = {"a": {"required": True}, "b": {"required": True}}
    assert validate_body({"body": {"a": 1, "b": 2}}, schema) == []


def test_validate_body_missing_required():
    schema = {"a": {"required": True}, "b": {"required": True}, "c": {"required": False}}
    assert sorted(validate_body({"body": {"a": 1}}, schema)) == ["b"]


def test_validate_body_non_dict_body_treated_as_empty():
    schema = {"a": {"required": True}}
    assert validate_body({"body": "not-a-dict"}, schema) == ["a"]
    assert validate_body({}, schema) == ["a"]


def test_validate_body_file_skipped_on_preflight():
    schema = {"img": {"type": "file", "required": True}}
    # Preflight (has_tx=False) must skip file fields — the file isn't sent yet.
    assert validate_body({"body": {}}, schema, has_tx=False) == []


def test_validate_body_file_required_on_execution():
    schema = {"img": {"type": "file", "required": True}}
    assert validate_body({"body": {}}, schema, has_tx=True) == ["img"]


def test_validate_body_file_satisfied_by_uploaded_files(tmp_path):
    schema = {"img": {"type": "file", "required": True}}
    uploaded = {"img": tmp_path / "fake.png"}
    assert validate_body({"body": {}}, schema, has_tx=True, uploaded_files=uploaded) == []


def test_validate_body_optional_file_never_missing():
    schema = {"img": {"type": "file", "required": False}}
    assert validate_body({"body": {}}, schema, has_tx=True) == []


# ── fetch_describe ─────────────────────────────────────────────────────

async def test_fetch_describe_success(monkeypatch):
    async def fake_run(**kwargs):
        return {
            "args_schema": {"text": {"type": "string", "required": True}},
            "result_schema": {"type": "object"},
        }
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    args_schema, result_schema = await fetch_describe("cmd", 10, "sid-1")
    assert args_schema == {"text": {"type": "string", "required": True}}
    assert result_schema == {"type": "object"}


async def test_fetch_describe_missing_args_schema_raises(monkeypatch):
    async def fake_run(**kwargs):
        return {}
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    with pytest.raises(RuntimeError, match="args_schema"):
        await fetch_describe("cmd", 10, "sid-1")


async def test_fetch_describe_invalid_args_schema_raises(monkeypatch):
    async def fake_run(**kwargs):
        return {"args_schema": "not a dict"}
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    with pytest.raises(RuntimeError):
        await fetch_describe("cmd", 10, "sid-1")


async def test_fetch_describe_non_dict_result_schema_becomes_none(monkeypatch):
    async def fake_run(**kwargs):
        return {"args_schema": {"a": {}}, "result_schema": "junk"}
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    _, result_schema = await fetch_describe("cmd", 10, "sid-1")
    assert result_schema is None


async def test_fetch_describe_subprocess_exception_wrapped(monkeypatch):
    async def fake_run(**kwargs):
        raise RuntimeError("agent crashed")
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)
    with pytest.raises(RuntimeError):
        await fetch_describe("cmd", 10, "sid-1")


# ── SidecarApp: file store helpers ─────────────────────────────────────

def test_process_file_result_passthrough_for_non_file(app_factory):
    app = app_factory()
    result = {"type": "text", "data": "hello"}
    assert app._process_file_result(result) == result


def test_process_file_result_missing_data_raises(app_factory):
    # BUG: _process_file_result silently returns a type=file result unchanged
    # when the 'data' key is absent, which means the sidecar happily forwards
    # a malformed file object (no URL, no bytes, no expiration) to the client.
    # Every other malformed branch (empty data, invalid base64, wrong type)
    # raises ValueError — this one should too. The current short-circuit
    # `if result.get("type") != "file" or "data" not in result: return result`
    # masks the malformed case.
    app = app_factory()
    with pytest.raises(ValueError):
        app._process_file_result({"type": "file"})


def test_process_file_result_writes_file_and_rewrites_url(app_factory):
    app = app_factory()
    payload = base64.b64encode(b"\x89PNG fake image bytes").decode()
    result = app._process_file_result(
        {"type": "file", "data": payload, "mime_type": "image/png", "file_name": "pic.png"}
    )
    assert result["type"] == "file"
    assert result["url"].startswith("/download/")
    assert result["mime_type"] == "image/png"
    assert result["file_name"] == "pic.png"
    file_id = result["url"].split("/")[-1]
    entry = app._file_store[file_id]
    assert Path(entry["path"]).exists()
    assert Path(entry["path"]).read_bytes() == b"\x89PNG fake image bytes"


def test_process_file_result_invalid_base64_raises(app_factory):
    app = app_factory()
    with pytest.raises(ValueError, match="invalid base64"):
        app._process_file_result({"type": "file", "data": "!!!not base64!!!"})


def test_process_file_result_non_string_data_raises(app_factory):
    app = app_factory()
    with pytest.raises(ValueError, match="non-empty base64 string"):
        app._process_file_result({"type": "file", "data": 123})


def test_process_file_result_empty_data_raises(app_factory):
    app = app_factory()
    with pytest.raises(ValueError, match="non-empty base64 string"):
        app._process_file_result({"type": "file", "data": ""})


def test_process_file_result_empty_decoded_bytes_raises(app_factory):
    app = app_factory()
    # "====" is a non-empty base64 string that decodes to zero bytes —
    # lets us reach the "empty bytes" branch past the length guard.
    with pytest.raises(ValueError, match="empty bytes"):
        app._process_file_result({"type": "file", "data": "===="})


def test_process_file_result_unknown_mime_has_no_extension(app_factory):
    app = app_factory()
    payload = base64.b64encode(b"data").decode()
    result = app._process_file_result({"type": "file", "data": payload, "mime_type": "weird/type"})
    assert Path(app._file_store[result["url"].split("/")[-1]]["path"]).suffix == ""


def test_cleanup_expired_files_removes_expired(app_factory):
    app = app_factory()
    # One fresh, one expired.
    fresh_path = app._file_store_dir / "fresh.bin"
    fresh_path.write_bytes(b"fresh")
    expired_path = app._file_store_dir / "expired.bin"
    expired_path.write_bytes(b"expired")

    app._file_store["fresh"] = {
        "path": str(fresh_path), "mime_type": "app/x", "file_name": "f",
        "expires_at": time.time() + 60,
    }
    app._file_store["expired"] = {
        "path": str(expired_path), "mime_type": "app/x", "file_name": "e",
        "expires_at": time.time() - 1,
    }

    app._cleanup_expired_files()
    assert "fresh" in app._file_store
    assert "expired" not in app._file_store
    assert fresh_path.exists()
    assert not expired_path.exists()


def test_cleanup_file_handles_missing_path(app_factory):
    app = app_factory()
    app._file_store["ghost"] = {
        "path": "/nonexistent/path/file.bin", "mime_type": "x", "file_name": "g",
        "expires_at": 0,
    }
    # Must not raise.
    app._cleanup_file("ghost")
    assert "ghost" not in app._file_store


def test_cleanup_file_unknown_id_is_noop(app_factory):
    app = app_factory()
    app._cleanup_file("never-existed")  # must not raise


# ── SidecarApp: result processing ──────────────────────────────────────

def test_safe_extract_result_unwraps_record_result(app_factory):
    app = app_factory()
    final, err = app._safe_extract_result({"result": {"type": "text", "data": "hi"}})
    assert err is None
    assert final == {"type": "text", "data": "hi"}


def test_safe_extract_result_passthrough_non_dict(app_factory):
    app = app_factory()
    final, err = app._safe_extract_result("raw string")
    assert err is None
    assert final == "raw string"


def test_safe_extract_result_catches_processing_error(app_factory):
    app = app_factory()
    # type=file with bad base64 → _process_file_result raises → wrapped into error string.
    final, err = app._safe_extract_result({"result": {"type": "file", "data": "!!!"}})
    assert final is None
    assert err == "Failed to process agent result"


def test_validate_result_structure_rejects_missing_keys():
    with pytest.raises(ValueError):
        SidecarApp._validate_result_structure({"result": {"type": "text"}})
    with pytest.raises(ValueError):
        SidecarApp._validate_result_structure({"result": {"data": "x"}})
    with pytest.raises(ValueError):
        SidecarApp._validate_result_structure({"result": "not a dict"})


def test_validate_result_structure_ok():
    SidecarApp._validate_result_structure({"result": {"type": "text", "data": "ok"}})


# ── SidecarApp: refund_user ────────────────────────────────────────────

async def test_refund_user_skipped_when_fee_exceeds_amount(app_factory, caplog):
    app = app_factory(refund_fee_nanoton=1_000_000)
    app.sender.send = AsyncMock()
    await app.refund_user("EQuser", payment_amount=500, original_tx_hash="txh", reason="timeout")
    app.sender.send.assert_not_awaited()


async def test_refund_user_sends_when_enough(app_factory):
    app = app_factory(refund_fee_nanoton=100)
    app.sender.send = AsyncMock(return_value="REFUND_HASH")
    await app.refund_user("EQuser", payment_amount=1_000, original_tx_hash="tx", reason="timeout")
    app.sender.send.assert_awaited_once()
    dest, amount, body = app.sender.send.call_args.args
    assert dest == "EQuser"
    assert amount == 900


async def test_refund_user_swallows_sender_exception(app_factory):
    app = app_factory(refund_fee_nanoton=100)
    app.sender.send = AsyncMock(side_effect=RuntimeError("ton down"))
    # Must not propagate — refunds are best-effort.
    await app.refund_user("EQuser", 1_000, "tx", "timeout")


# ── SidecarApp: quote helpers ──────────────────────────────────────────

def test_cleanup_expired_quotes_removes_expired(app_factory):
    app = app_factory()
    app.quotes["old"] = QuoteEntry(price=100, expires_at=time.time() - 1, sku_id=DEFAULT_SKU_ID)
    app.quotes["new"] = QuoteEntry(price=200, expires_at=time.time() + 60, sku_id=DEFAULT_SKU_ID)
    app._cleanup_expired_quotes()
    assert "old" not in app.quotes
    assert "new" in app.quotes


# ── HTTP handlers via aiohttp test client ──────────────────────────────

@pytest.fixture
async def client(app_factory, tmp_path):
    """Build a mocked SidecarApp and serve it via aiohttp TestClient.

    startup() is stubbed out so no TON liteservers or subprocesses are contacted.
    """
    app = app_factory()
    app.args_schema = {"text": {"type": "string", "required": True}}

    async def noop_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop_startup
    app.shutdown = noop_shutdown
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop_startup())
    web_app.on_shutdown.append(lambda _: noop_shutdown())

    async with TestClient(TestServer(web_app)) as c:
        c.sidecar = app  # type: ignore[attr-defined]
        yield c


async def test_info_handler_returns_metadata(client):
    resp = await client.get("/info")
    assert resp.status == 200
    data = await resp.json()
    assert data["name"] == "Translator"
    assert data["capabilities"] == ["translate"]
    assert data["sidecar_id"] == "sid-test"
    assert "args_schema" in data
    # Per-SKU prices live under skus[]; legacy top-level price/price_usdt are gone.
    assert "price" not in data
    assert data["skus"][0]["price_ton"] == 1_000_000


async def test_options_request_returns_cors_204(client):
    resp = await client.options("/invoke")
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]


async def test_cors_headers_appended_on_responses(client):
    resp = await client.get("/info")
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


async def test_invoke_missing_capability_returns_400(client):
    resp = await client.post("/invoke", json={})
    assert resp.status == 400
    data = await resp.json()
    assert "capability is required" in data["error"]


async def test_invoke_wrong_capability_returns_400(client):
    resp = await client.post("/invoke", json={"capability": "other"})
    assert resp.status == 400


async def test_invoke_preflight_returns_402_with_payment_info(client):
    resp = await client.post("/invoke", json={"capability": "translate"})
    assert resp.status == 402
    assert resp.headers["x-ton-pay-address"] == "EQagent"
    assert resp.headers["x-ton-pay-amount"] == "1000000"
    nonce = resp.headers["x-ton-pay-nonce"]
    assert nonce.endswith(":sid-test")
    data = await resp.json()
    assert data["payment_request"]["address"] == "EQagent"
    assert data["payment_request"]["amount"] == "1000000"


async def test_invoke_preflight_preserves_valid_nonce(client):
    client_nonce = "userchosen:sid-test"
    resp = await client.post(
        "/invoke",
        json={"capability": "translate", "nonce": client_nonce},
    )
    assert resp.status == 402
    assert resp.headers["x-ton-pay-nonce"] == client_nonce


async def test_invoke_preflight_rewrites_bad_nonce(client):
    resp = await client.post(
        "/invoke",
        json={"capability": "translate", "nonce": "clientonly"},
    )
    assert resp.status == 402
    # Server refused the caller's nonce and issued one bound to this sidecar.
    assert resp.headers["x-ton-pay-nonce"] != "clientonly"
    assert resp.headers["x-ton-pay-nonce"].endswith(":sid-test")


async def test_invoke_invalid_json_returns_400(client):
    resp = await client.post("/invoke", data="not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_invoke_missing_nonce_with_tx_returns_400(client):
    resp = await client.post(
        "/invoke",
        json={"capability": "translate", "tx": "abc", "body": {"text": "hi"}},
    )
    assert resp.status == 400
    data = await resp.json()
    assert "nonce is required" in data["error"]


async def test_invoke_missing_required_body_field_returns_400(client):
    resp = await client.post(
        "/invoke",
        json={
            "capability": "translate",
            "tx": "txh",
            "nonce": "n:sid-test",
            "body": {},  # missing 'text'
        },
    )
    assert resp.status == 400
    data = await resp.json()
    assert "missing" in data
    assert "text" in data["missing"]


async def test_invoke_nonce_sidecar_mismatch_returns_402(client):
    resp = await client.post(
        "/invoke",
        json={
            "capability": "translate",
            "tx": "txh",
            "nonce": "n:wrong-sid",
            "body": {"text": "hi"},
        },
    )
    assert resp.status == 402


async def test_invoke_already_processed_tx_returns_409(client):
    app: SidecarApp = client.sidecar
    app.tx_store.is_processed = AsyncMock(return_value=True)
    resp = await client.post(
        "/invoke",
        json={
            "capability": "translate",
            "tx": "dup-tx",
            "nonce": "n:sid-test",
            "body": {"text": "hi"},
        },
    )
    assert resp.status == 409


async def test_invoke_payment_verification_error_returns_402(client):
    app: SidecarApp = client.sidecar
    app.tx_store.is_processed = AsyncMock(return_value=False)
    app.verifier.verify = AsyncMock(side_effect=PaymentVerificationError("bad"))
    resp = await client.post(
        "/invoke",
        json={
            "capability": "translate",
            "tx": "txh",
            "nonce": "n:sid-test",
            "body": {"text": "hi"},
        },
    )
    assert resp.status == 402
    data = await resp.json()
    assert data["error"] == "bad"


async def test_invoke_payment_verification_unexpected_enqueues_refund(client):
    """When the verifier raises an unknown exception (not PaymentVerificationError),
    we can't tell if the tx is real. The handler enqueues a refund and returns
    503 refund_pending so the worker can recover via the monitor."""
    app: SidecarApp = client.sidecar
    await app.refund_queue.init()
    try:
        app.tx_store.is_processed = AsyncMock(return_value=False)
        app.verifier.verify = AsyncMock(side_effect=RuntimeError("rpc down"))
        resp = await client.post(
            "/invoke",
            json={
                "capability": "translate",
                "tx": "txh",
                "nonce": "n:sid-test",
                "body": {"text": "hi"},
            },
        )
        assert resp.status == 503
        data = await resp.json()
        assert data["refund_pending"] is True
        entry = await app.refund_queue.get("txh")
        assert entry is not None
        assert entry.status == "pending"
        assert entry.force_refund == 0  # pre-verify, not a force case
    finally:
        await app.refund_queue.close()


async def test_invoke_happy_path_runs_agent_and_returns_done(client, monkeypatch):
    app: SidecarApp = client.sidecar
    app.tx_store.is_processed = AsyncMock(return_value=False)
    app.tx_store.mark_processed = AsyncMock()
    app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
        tx_hash="real-hash", sender="EQsender", recipient="EQagent",
        amount=5_000_000, comment="n:sid-test",
    ))

    async def fake_run(**kwargs):
        return {"result": {"type": "text", "data": "translated"}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    resp = await client.post(
        "/invoke",
        json={
            "capability": "translate",
            "tx": "user-tx",
            "nonce": "n:sid-test",
            "body": {"text": "hello"},
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "done"
    assert data["result"] == {"type": "text", "data": "translated"}
    # The mark was against the real on-chain hash, not the user-supplied one.
    app.tx_store.mark_processed.assert_awaited_once_with("real-hash")


async def test_invoke_agent_runtime_error_triggers_refund(client, monkeypatch):
    app: SidecarApp = client.sidecar
    app.tx_store.is_processed = AsyncMock(return_value=False)
    app.tx_store.mark_processed = AsyncMock()
    app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
        tx_hash="real-hash", sender="EQsender", recipient="EQagent",
        amount=5_000_000, comment="n:sid-test",
    ))
    app.sender.send = AsyncMock(return_value="REFUND_HASH")

    async def fake_run(**kwargs):
        raise RuntimeError("agent died")

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    resp = await client.post(
        "/invoke",
        json={
            "capability": "translate",
            "tx": "user-tx",
            "nonce": "n:sid-test",
            "body": {"text": "hello"},
        },
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "refunded"
    assert data["reason_code"] == "execution_failed"
    assert data["refund_tx"] == "REFUND_HASH"
    # Refund was issued back to the sender.
    await asyncio.sleep(0.05)
    app.sender.send.assert_awaited()
    dest, amount, _ = app.sender.send.call_args.args
    assert dest == "EQsender"
    assert amount == 5_000_000 - 500_000  # refund_fee_nanoton


# ── handle_result ──────────────────────────────────────────────────────

async def test_handle_result_unknown_job_returns_404(client):
    resp = await client.get("/result/ghost-job")
    assert resp.status == 404


async def test_handle_result_returns_status(client):
    app: SidecarApp = client.sidecar

    async def done_runner():
        return {"result": {"type": "text", "data": "hi"}}

    job_id = await app.jobs.submit(done_runner)
    await app.jobs.wait_for_completion(job_id, timeout_seconds=5)

    resp = await client.get(f"/result/{job_id}")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "done"
    assert data["result"] == {"type": "text", "data": "hi"}


# ── handle_download ────────────────────────────────────────────────────

async def test_handle_download_unknown_returns_404(client):
    resp = await client.get("/download/ghost")
    assert resp.status == 404


async def test_handle_download_expired_returns_410(client):
    app: SidecarApp = client.sidecar
    path = app._file_store_dir / "x.bin"
    path.write_bytes(b"data")
    app._file_store["exp"] = {
        "path": str(path), "mime_type": "text/plain", "file_name": "x.bin",
        "expires_at": time.time() - 1,
    }
    resp = await client.get("/download/exp")
    assert resp.status == 410
    # And it cleaned up behind itself.
    assert "exp" not in app._file_store


async def test_handle_download_success(client):
    app: SidecarApp = client.sidecar
    path = app._file_store_dir / "ok.bin"
    path.write_bytes(b"binary-data")
    app._file_store["ok"] = {
        "path": str(path), "mime_type": "application/octet-stream", "file_name": "ok.bin",
        "expires_at": time.time() + 60,
    }
    resp = await client.get("/download/ok")
    assert resp.status == 200
    assert await resp.read() == b"binary-data"
    assert "inline" in resp.headers["Content-Disposition"]


async def test_handle_download_missing_file_on_disk_returns_404(client):
    app: SidecarApp = client.sidecar
    app._file_store["gone"] = {
        "path": "/nonexistent/not-here.bin",
        "mime_type": "text/plain", "file_name": "gone",
        "expires_at": time.time() + 60,
    }
    resp = await client.get("/download/gone")
    assert resp.status == 404


# ── Quote handler ──────────────────────────────────────────────────────

async def test_handle_quote_disabled_returns_404(client):
    resp = await client.post("/quote", json={"capability": "translate", "body": {"text": "hi"}})
    assert resp.status == 404


async def test_handle_quote_enabled_returns_quote(app_factory, monkeypatch):
    app = app_factory(has_quote=True)
    app.args_schema = {"text": {"type": "string", "required": True}}

    async def noop():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop  # type: ignore[method-assign]
    app.shutdown = noop_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop())
    web_app.on_shutdown.append(lambda _: noop_shutdown())

    async def fake_run(**kwargs):
        return {"price": 7_777, "plan": "cheap", "ttl": 30}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/quote", json={"capability": "translate", "body": {"text": "hi"}})
        assert resp.status == 200
        data = await resp.json()
        assert data["price"] == 7_777
        assert data["plan"] == "cheap"
        assert "quote_id" in data
        assert data["quote_id"] in app.quotes


async def test_handle_quote_invalid_price_returns_500(app_factory, monkeypatch):
    app = app_factory(has_quote=True)
    app.args_schema = {"text": {"type": "string", "required": True}}

    async def noop():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop  # type: ignore[method-assign]
    app.shutdown = noop_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop())
    web_app.on_shutdown.append(lambda _: noop_shutdown())

    async def fake_run(**kwargs):
        return {"price": -1, "plan": ""}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/quote", json={"capability": "translate", "body": {"text": "hi"}})
        assert resp.status == 500


async def test_handle_quote_missing_required_body_returns_400(app_factory):
    app = app_factory(has_quote=True)
    app.args_schema = {"text": {"type": "string", "required": True}}

    async def noop():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop  # type: ignore[method-assign]
    app.shutdown = noop_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop())
    web_app.on_shutdown.append(lambda _: noop_shutdown())

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/quote", json={"capability": "translate", "body": {}})
        assert resp.status == 400


# ── Rate limiting middleware ───────────────────────────────────────────

async def test_rate_limit_blocks_after_threshold(client):
    # settings: rate_limit_requests=3, window=10
    for _ in range(3):
        resp = await client.get("/invoke")  # GET on /invoke -> method not allowed but still goes through middleware
        # /invoke is POST-only so GET returns 405 — but that still counts against the limit.
    resp = await client.post("/invoke", json={"capability": "translate"})
    assert resp.status == 429
    data = await resp.json()
    assert "Too many requests" in data["error"]


async def test_rate_limit_bypasses_info(client):
    # /info is explicitly exempt — should never 429.
    for _ in range(20):
        resp = await client.get("/info")
        assert resp.status == 200


async def test_rate_limit_bypasses_downloads(client):
    app: SidecarApp = client.sidecar
    path = app._file_store_dir / "dl.bin"
    path.write_bytes(b"x")
    app._file_store["dl"] = {
        "path": str(path), "mime_type": "text/plain", "file_name": "dl.bin",
        "expires_at": time.time() + 60,
    }
    for _ in range(20):
        resp = await client.get("/download/dl")
        assert resp.status == 200


# ── Multipart invoke parsing ──────────────────────────────────────────

async def test_invoke_multipart_preflight_returns_402(client):
    # aiohttp only serialises FormData as multipart when at least one part
    # is a file — include a stub file part to force multipart encoding.
    form = FormData()
    form.add_field("capability", "translate")
    form.add_field("body_json", json.dumps({}))
    form.add_field("marker", io.BytesIO(b""), filename="marker", content_type="application/octet-stream")
    resp = await client.post("/invoke", data=form)
    assert resp.status == 402


async def test_invoke_multipart_with_file_upload_happy_path(client, monkeypatch):
    app: SidecarApp = client.sidecar
    app.args_schema = {
        "text": {"type": "string", "required": True},
        "image": {"type": "file", "required": True},
    }
    app.tx_store.is_processed = AsyncMock(return_value=False)
    app.tx_store.mark_processed = AsyncMock()
    app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
        tx_hash="real", sender="EQsender", recipient="EQagent",
        amount=5_000_000, comment="n:sid-test",
    ))

    captured_payload: dict[str, Any] = {}

    async def fake_run(**kwargs):
        captured_payload.update(kwargs.get("payload", {}))
        return {"result": {"type": "text", "data": "ok"}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    form = FormData()
    form.add_field("capability", "translate")
    form.add_field("tx", "user-tx")
    form.add_field("nonce", "n:sid-test")
    form.add_field("body_json", json.dumps({"text": "hi"}))
    form.add_field("file:image", io.BytesIO(b"FAKE PNG"), filename="pic.png", content_type="image/png")

    resp = await client.post("/invoke", data=form)
    assert resp.status == 200
    # Agent was called with the file path injected into the body.
    assert "image_path" in captured_payload.get("body", {})
    assert captured_payload["body"]["image_name"] == "pic.png"


# ── Stock / SKU integration ────────────────────────────────────────────

async def _stock_client(app_factory, tmp_path, initial_stock: int = 1):
    """Build a TestClient for an app whose default SKU has a tracked stock."""
    from settings import AgentSku, DEFAULT_SKU_ID
    sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID,
        price_ton=1_000_000, price_usd=None, initial_stock=initial_stock,
    )
    app = app_factory(skus=(sku,), payment_rails=("TON",))
    app.args_schema = {"text": {"type": "string", "required": True}}

    async def noop_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop_startup  # type: ignore[method-assign]
    app.shutdown = noop_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop_startup())
    web_app.on_shutdown.append(lambda _: noop_shutdown())
    return app, TestClient(TestServer(web_app))


async def test_invoke_preflight_returns_409_when_out_of_stock(app_factory, tmp_path):
    app, tc = await _stock_client(app_factory, tmp_path, initial_stock=0)
    async with tc as c:
        resp = await c.post("/invoke", json={"capability": "translate"})
        assert resp.status == 409
        data = await resp.json()
        assert data["error"] == "out_of_stock"


async def test_invoke_with_tracked_stock_reserves_and_commits_on_success(app_factory, tmp_path, monkeypatch):
    app, tc = await _stock_client(app_factory, tmp_path, initial_stock=2)
    async with tc as c:
        app.tx_store.is_processed = AsyncMock(return_value=False)
        app.tx_store.mark_processed = AsyncMock()
        app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
            tx_hash="real-hash", sender="EQsender", recipient="EQagent",
            amount=1_000_000, comment="n:sid-test",
        ))

        async def fake_run(**kwargs):
            return {"result": {"type": "text", "data": "ok"}}

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "u", "nonce": "n:sid-test",
            "body": {"text": "hi"},
        })
        assert resp.status == 200
        view = await app.stock.get_view("default")
        assert view.sold == 1
        assert view.reserved == 0
        assert view.stock_left == 1


async def test_invoke_out_of_stock_from_agent_refunds_and_reports(app_factory, tmp_path, monkeypatch):
    app, tc = await _stock_client(app_factory, tmp_path, initial_stock=1)
    async with tc as c:
        app.tx_store.is_processed = AsyncMock(return_value=False)
        app.tx_store.mark_processed = AsyncMock()
        app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
            tx_hash="real-hash", sender="EQsender", recipient="EQagent",
            amount=1_000_000, comment="n:sid-test",
        ))
        app.sender.send = AsyncMock(return_value="REFUND_HASH")

        async def fake_run(**kwargs):
            return {"error": "out_of_stock", "reason": "banned before delivery"}

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "u", "nonce": "n:sid-test",
            "body": {"text": "hi"},
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "refunded"
        assert data["reason_code"] == "out_of_stock"
        assert data["reason"] == "banned before delivery"
        assert data["refund_tx"] == "REFUND_HASH"

        view = await app.stock.get_view("default")
        # Agent reported unit is gone → total decremented.
        assert view.total == 0
        assert view.sold == 0
        assert view.reserved == 0


async def test_invoke_agent_failure_releases_reservation(app_factory, tmp_path, monkeypatch):
    app, tc = await _stock_client(app_factory, tmp_path, initial_stock=3)
    async with tc as c:
        app.tx_store.is_processed = AsyncMock(return_value=False)
        app.tx_store.mark_processed = AsyncMock()
        app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
            tx_hash="real-hash", sender="EQsender", recipient="EQagent",
            amount=1_000_000, comment="n:sid-test",
        ))
        app.sender.send = AsyncMock(return_value="REFUND_HASH")

        async def fake_run(**kwargs):
            raise RuntimeError("agent died")

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "u", "nonce": "n:sid-test",
            "body": {"text": "hi"},
        })
        # Refund succeeded → runner reports refunded, not error.
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "refunded"
        assert data["reason_code"] == "execution_failed"
        assert data["refund_tx"] == "REFUND_HASH"
        await asyncio.sleep(0.05)
        view = await app.stock.get_view("default")
        # Stock untouched — total still 3, nothing reserved or sold.
        assert view.total == 3
        assert view.sold == 0
        assert view.reserved == 0


async def test_info_reports_skus(app_factory, tmp_path):
    app, tc = await _stock_client(app_factory, tmp_path, initial_stock=5)
    async with tc as c:
        resp = await c.get("/info")
        assert resp.status == 200
        data = await resp.json()
        assert "skus" in data
        assert len(data["skus"]) == 1
        entry = data["skus"][0]
        assert entry["id"] == "default"
        assert entry["price_ton"] == 1_000_000
        assert entry["stock_left"] == 5
        assert entry["total"] == 5
        assert entry["sold"] == 0


async def test_invoke_unknown_sku_returns_400(app_factory, tmp_path):
    from settings import AgentSku
    skus = (
        AgentSku(sku_id="a", title="A", price_ton=1_000_000, price_usd=None, initial_stock=None),
        AgentSku(sku_id="b", title="B", price_ton=2_000_000, price_usd=None, initial_stock=None),
    )
    app = app_factory(skus=skus, payment_rails=("TON",))
    app.args_schema = {"text": {"type": "string", "required": True}}

    async def noop_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop_startup  # type: ignore[method-assign]
    app.shutdown = noop_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop_startup())
    web_app.on_shutdown.append(lambda _: noop_shutdown())

    async with TestClient(TestServer(web_app)) as c:
        # Missing sku field with multiple SKUs configured → 400.
        resp = await c.post("/invoke", json={"capability": "translate"})
        assert resp.status == 400
        # Unknown sku → 400.
        resp = await c.post("/invoke", json={"capability": "translate", "sku": "ghost"})
        assert resp.status == 400


# ── Dynamic pricing ────────────────────────────────────────────────────


def _make_dynamic_app(app_factory, tmp_path, sku_ids: list[str]) -> "SidecarApp":
    """Build a SidecarApp with price_ton=0 AND price_usd=0 SKUs (dynamic pricing sentinel)."""
    skus = tuple(
        AgentSku(sku_id=sid, title=sid, price_ton=0, price_usd=0, initial_stock=None)
        for sid in sku_ids
    )
    app = app_factory(skus=skus, payment_rails=("TON",))
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)
    return app


async def _dynamic_test_client(app_factory, tmp_path, sku_ids: list[str]):
    app = _make_dynamic_app(app_factory, tmp_path, sku_ids)

    async def noop_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)

    async def noop_shutdown():
        await _close_app_stores(app)

    app.startup = noop_startup  # type: ignore[method-assign]
    app.shutdown = noop_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: noop_startup())
    web_app.on_shutdown.append(lambda _: noop_shutdown())
    return app, TestClient(TestServer(web_app))


async def test_info_with_dynamic_sku_shows_fetched_prices(app_factory, tmp_path, monkeypatch):
    app, tc = await _dynamic_test_client(app_factory, tmp_path, ["premium_3m", "premium_6m"])

    async def fake_run(**kwargs):
        assert kwargs["payload"]["mode"] == "prices"
        return {"prices": {"premium_3m": {"ton": 42_000_000_000}, "premium_6m": {"ton": 75_000_000_000}}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async with tc as c:
        resp = await c.get("/info")
        assert resp.status == 200
        data = await resp.json()
        skus = {s["id"]: s for s in data.get("skus", [])}
        assert skus["premium_3m"]["price_ton"] == 42_000_000_000
        assert skus["premium_6m"]["price_ton"] == 75_000_000_000


async def test_info_with_dynamic_sku_uses_cache_on_second_call(app_factory, tmp_path, monkeypatch):
    app, tc = await _dynamic_test_client(app_factory, tmp_path, ["premium_3m"])
    call_count = 0

    async def fake_run(**kwargs):
        nonlocal call_count
        call_count += 1
        return {"prices": {"premium_3m": {"ton": 10_000_000_000}}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async with tc as c:
        await c.get("/info")
        await c.get("/info")
        # Cache TTL not expired → agent called only once.
        assert call_count == 1


async def test_info_with_dynamic_sku_agent_failure_shows_no_price(app_factory, tmp_path, monkeypatch):
    app, tc = await _dynamic_test_client(app_factory, tmp_path, ["premium_3m"])

    async def fake_run(**kwargs):
        raise RuntimeError("agent down")

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async with tc as c:
        resp = await c.get("/info")
        assert resp.status == 200
        data = await resp.json()
        # Price absent (agent failed, cache empty) — no crash.
        sku_entry = next(s for s in data.get("skus", []) if s["id"] == "premium_3m")
        assert "price_ton" not in sku_entry


async def test_invoke_preflight_dynamic_sku_shows_fetched_price(app_factory, tmp_path, monkeypatch):
    app, tc = await _dynamic_test_client(app_factory, tmp_path, ["premium_3m"])

    async def fake_run(**kwargs):
        return {"prices": {"premium_3m": {"ton": 50_000_000_000}}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async with tc as c:
        resp = await c.post("/invoke", json={"capability": "translate", "sku": "premium_3m"})
        assert resp.status == 402
        data = await resp.json()
        option = data["payment_options"][0]
        assert option["amount"] == "50000000000"
        assert resp.headers["x-ton-pay-amount"] == "50000000000"
