"""Tests for chains/ton/broadcast.py — toncenter first, TonAPI as fallback."""

from __future__ import annotations

import base64

import pytest
from aiohttp import web

from chains.ton.broadcast import BroadcastError, HttpBroadcaster

BOC = bytes.fromhex("b5ee9c72")


async def _serve(server_factory_, toncenter_resp, tonapi_resp, calls):
    async def toncenter(request):
        calls.append(("toncenter", await request.json(), request.headers.get("X-API-Key")))
        status, body = toncenter_resp
        return web.json_response(body, status=status)

    async def tonapi(request):
        calls.append(("tonapi", await request.json(), request.headers.get("Authorization")))
        status, body = tonapi_resp
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/api/v2/sendBoc", toncenter)
    app.router.add_post("/v2/blockchain/message", tonapi)
    return await server_factory_(app)


@pytest.fixture
async def server_factory():
    runners = []

    async def factory(app):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield factory
    for r in runners:
        await r.cleanup()


def _broadcaster(base: str, monkeypatch) -> HttpBroadcaster:
    monkeypatch.setenv("TONCENTER_API_KEY", "tc-key")
    monkeypatch.setenv("TONAPI_KEY", "ta-key")
    monkeypatch.setenv("TONAPI_BASE", base)
    b = HttpBroadcaster(testnet=False)
    b._toncenter_base = base
    return b


async def test_toncenter_first_and_tonapi_untouched(server_factory, monkeypatch):
    calls = []
    base = await _serve(server_factory, (200, {"ok": True}), (200, {}), calls)
    b = _broadcaster(base, monkeypatch)

    assert await b.send_boc(BOC) == "toncenter"
    assert calls == [("toncenter", {"boc": base64.b64encode(BOC).decode()}, "tc-key")]


async def test_falls_back_to_tonapi_on_toncenter_error(server_factory, monkeypatch):
    calls = []
    base = await _serve(server_factory, (500, {"ok": False, "error": "boom"}), (200, {}), calls)
    b = _broadcaster(base, monkeypatch)

    assert await b.send_boc(BOC) == "tonapi"
    assert [c[0] for c in calls] == ["toncenter", "tonapi"]
    assert calls[1][2] == "Bearer ta-key"


async def test_toncenter_ok_false_counts_as_failure(server_factory, monkeypatch):
    calls = []
    base = await _serve(server_factory, (200, {"ok": False, "error": "bad boc"}), (200, {}), calls)
    b = _broadcaster(base, monkeypatch)

    assert await b.send_boc(BOC) == "tonapi"


async def test_raises_when_both_fail(server_factory, monkeypatch):
    calls = []
    base = await _serve(server_factory, (429, {"ok": False}), (400, {"error": "x"}), calls)
    b = _broadcaster(base, monkeypatch)

    with pytest.raises(BroadcastError, match="toncenter.*tonapi"):
        await b.send_boc(BOC)
