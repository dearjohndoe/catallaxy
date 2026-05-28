"""Tests for the TonAPI fallback (plan C from LITESERVER_RESILIENCE_TASK.md)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytoniq_core import begin_cell

from payments.tonapi_client import TonAPIClient, _wrap_tx
from payments.ton_monitor import WalletMonitor
from transfer import PAYMENT_OPCODE


def _payment_body_hex(nonce: str) -> str:
    return (
        begin_cell()
        .store_uint(PAYMENT_OPCODE, 32)
        .store_snake_string(nonce)
        .end_cell()
        .to_boc()
        .hex()
    )


def _tonapi_tx(*, lt: int, utime: int, nonce: str, hash_hex: str | None = None) -> dict:
    return {
        "lt": lt,
        "utime": utime,
        "hash": hash_hex or ("a" * 64),
        "in_msg": {
            "msg_type": "int_msg",
            "value": "100000000",
            "source": {"address": "0:" + "7c" * 32},
            "destination": {"address": "0:" + "11" * 32},
            "raw_body": _payment_body_hex(nonce),
        },
    }


# ── _wrap_tx ───────────────────────────────────────────────────────────


def test_wrap_tx_extracts_fields():
    nonce = "abc123:deadbeef"
    raw = _tonapi_tx(lt=42, utime=1748290000, nonce=nonce)

    tx = _wrap_tx(raw)
    assert tx is not None
    assert tx.lt == 42
    assert tx.now == 1748290000
    assert tx.cell.hash == bytes.fromhex("a" * 64)
    assert tx.in_msg is not None
    assert tx.in_msg.info.value.grams == 100_000_000

    from payments.nonce import _parse_payment_nonce
    assert _parse_payment_nonce(tx.in_msg.body) == nonce


def test_wrap_tx_handles_external_in():
    raw = {
        "lt": 1,
        "utime": 1,
        "hash": "b" * 64,
        "in_msg": {"msg_type": "ext_in_msg", "value": "0", "source": {"address": ""}},
    }
    tx = _wrap_tx(raw)
    assert tx is not None
    assert tx.in_msg.is_external is True
    assert tx.in_msg.body is None


def test_wrap_tx_returns_none_on_garbage():
    assert _wrap_tx({"hash": "not-hex"}) is None  # no lt
    assert _wrap_tx({"lt": "abc", "hash": "aa"}) is None


# ── TonAPIClient HTTP layer ────────────────────────────────────────────


async def _serve(handler):
    """Spin up a local aiohttp test server. Returns (base_url, cleanup)."""
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/v2/blockchain/accounts/{addr}/transactions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    async def cleanup() -> None:
        await runner.cleanup()
    return base, cleanup


@pytest.mark.asyncio
async def test_tonapi_client_get_account_transactions_parses_response():
    from aiohttp import web

    received: dict = {}

    async def handler(request: web.Request) -> web.Response:
        received["path"] = request.path
        received["limit"] = request.query.get("limit")
        received["auth"] = request.headers.get("Authorization")
        return web.json_response({
            "transactions": [
                _tonapi_tx(lt=2, utime=int(time.time()), nonce="nonce-b"),
                _tonapi_tx(lt=1, utime=int(time.time()), nonce="nonce-a"),
            ],
        })

    base, cleanup = await _serve(handler)
    client = TonAPIClient(base_url=base, api_key="testkey")
    try:
        txs = await client.get_account_transactions("UQTest", limit=10)
    finally:
        await client.close()
        await cleanup()

    assert received["path"].endswith("/UQTest/transactions")
    assert received["limit"] == "10"
    assert received["auth"] == "Bearer testkey"
    assert len(txs) == 2
    assert [t.lt for t in txs] == [2, 1]


@pytest.mark.asyncio
async def test_tonapi_client_raises_rate_limit():
    from aiohttp import web
    from payments.tonapi_client import TonAPIRateLimitError

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=429, text="slow down")

    base, cleanup = await _serve(handler)
    client = TonAPIClient(base_url=base)
    try:
        with pytest.raises(TonAPIRateLimitError):
            await client.get_account_transactions("UQAddr")
    finally:
        await client.close()
        await cleanup()


# ── WalletMonitor fallback path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_wallet_monitor_falls_back_to_tonapi_on_adnl_failure():
    adnl_client = MagicMock()
    adnl_client.get_transactions = AsyncMock(side_effect=RuntimeError("balancer down"))

    tonapi = MagicMock()
    fallback_tx = _wrap_tx(_tonapi_tx(lt=99, utime=int(time.time()), nonce="recovered"))
    tonapi.get_account_transactions = AsyncMock(return_value=[fallback_tx])

    monitor = WalletMonitor(
        client=adnl_client,
        address="UQAgent",
        poll_interval=60,
        tonapi_client=tonapi,
    )

    await monitor._poll()

    adnl_client.get_transactions.assert_awaited_once()
    tonapi.get_account_transactions.assert_awaited_once_with("UQAgent", limit=50)
    assert monitor._consecutive_lite_errors == 1
    assert monitor._last_successful_poll_at > 0
    assert await monitor.get("recovered") is fallback_tx


@pytest.mark.asyncio
async def test_wallet_monitor_skips_fallback_when_adnl_succeeds():
    adnl_tx = _wrap_tx(_tonapi_tx(lt=10, utime=int(time.time()), nonce="ok-via-adnl"))
    adnl_client = MagicMock()
    adnl_client.get_transactions = AsyncMock(return_value=[adnl_tx])

    tonapi = MagicMock()
    tonapi.get_account_transactions = AsyncMock(return_value=[])

    monitor = WalletMonitor(
        client=adnl_client, address="UQAgent", poll_interval=60, tonapi_client=tonapi,
    )

    await monitor._poll()

    tonapi.get_account_transactions.assert_not_called()
    assert monitor._consecutive_lite_errors == 0
    assert await monitor.get("ok-via-adnl") is adnl_tx


@pytest.mark.asyncio
async def test_wallet_monitor_no_fallback_without_client():
    """No TonAPI client → ADNL failure is silently logged, no crash."""
    adnl_client = MagicMock()
    adnl_client.get_transactions = AsyncMock(side_effect=RuntimeError("balancer down"))

    monitor = WalletMonitor(
        client=adnl_client, address="UQAgent", poll_interval=60, tonapi_client=None,
    )

    await monitor._poll()  # must not raise

    assert monitor._consecutive_lite_errors == 1
    assert monitor._last_successful_poll_at == 0.0


# ── WalletMonitor.is_healthy (plan D) ──────────────────────────────────


def test_wallet_monitor_is_unhealthy_before_first_poll():
    monitor = WalletMonitor(
        client=MagicMock(), address="UQAgent", poll_interval=60,
    )
    assert monitor.is_healthy() is False


def test_wallet_monitor_is_healthy_right_after_successful_poll():
    monitor = WalletMonitor(
        client=MagicMock(), address="UQAgent", poll_interval=60,
    )
    monitor._last_successful_poll_at = time.time()
    assert monitor.is_healthy(max_age_seconds=60) is True


def test_wallet_monitor_is_unhealthy_after_staleness_window():
    monitor = WalletMonitor(
        client=MagicMock(), address="UQAgent", poll_interval=60,
    )
    monitor._last_successful_poll_at = time.time() - 120
    assert monitor.is_healthy(max_age_seconds=60) is False
