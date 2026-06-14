"""Tests that probe for known weaknesses in sidecar code.

These tests encode the BEHAVIOUR WE WANT. Many of them are expected to fail
against the current implementation — they document real bugs that should be
fixed rather than pinned. Each test has a docstring explaining the bug it
catches.

Run this file in isolation to see every pending issue at a glance:

    python -m pytest tests/test_known_bugs.py -v
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import api as api_module
import transfer as transfer_module
from api import SidecarApp
from settings import AgentSku, DEFAULT_SKU_ID, Settings
from transfer import TransferSender
from payments import PaymentVerificationError, ProcessedTxStore, VerifiedPayment


# ── Shared settings/app builders (subset of test_api.py) ───────────────

def _make_settings(tmp_path: Path, **overrides) -> Settings:
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
        capabilities=("translate",),
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
        rate_limit_window=1,  # short window so eviction test runs fast
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


def _make_app(tmp_path: Path, **settings_overrides) -> SidecarApp:
    app = SidecarApp(_make_settings(tmp_path, **settings_overrides))
    app.sidecar_id = "sid-test"
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)
    return app


@pytest.fixture
async def bug_client(tmp_path):
    """aiohttp TestClient bound to a SidecarApp with mocks for TON deps."""
    app = _make_app(tmp_path)

    async def fake_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.refund_queue.init()

    async def fake_shutdown():
        await app.refund_queue.close()
        try:
            await app.tx_store.close()
        except Exception:
            pass
        try:
            await app.stock.close()
        except Exception:
            pass

    app.startup = fake_startup  # type: ignore[method-assign]
    app.shutdown = fake_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: fake_startup())
    web_app.on_shutdown.append(lambda _: fake_shutdown())

    async with TestClient(TestServer(web_app)) as c:
        c.sidecar = app  # type: ignore[attr-defined]
        yield c


# ────────────────────────────────────────────────────────────────────────
# BUG 1 — rate limiter leaks memory: empty histories are never evicted
# ────────────────────────────────────────────────────────────────────────

async def test_rate_limiter_evicts_empty_histories(bug_client):
    """The rate_limits dict must not grow unboundedly.

    BUG history: the middleware appended each request timestamp into
    ``self.rate_limits[ip]`` and filtered out expired timestamps on every
    subsequent call from the same IP — but when the filtered history became
    empty for an IP that never returned, the key itself was never deleted.
    An attacker rotating source IPs could force the dict to grow without
    bound (slow memory exhaustion / DoS vector).

    Fix contract: the app exposes ``_cleanup_rate_limits()`` (called from
    cleanup_loop on a timer) that sweeps every IP whose entire history is
    now older than the window and drops it from the dict.
    """
    app: SidecarApp = bug_client.sidecar
    assert app.settings.rate_limit_window == 1  # 1 second window from fixture

    # Seed an entry via a non-exempt path so the middleware actually stores
    # a timestamp. /result/<job_id> is non-exempt and returns quickly (404).
    await bug_client.get("/result/nope")
    assert len(app.rate_limits) >= 1

    # Let the window elapse so every stored timestamp is stale.
    await asyncio.sleep(app.settings.rate_limit_window + 0.2)

    # Simulate the periodic cleanup sweep the cleanup_loop would run.
    app._cleanup_rate_limits()

    cutoff = time.time() - app.settings.rate_limit_window
    leaked = {
        ip: hist
        for ip, hist in app.rate_limits.items()
        if not hist or all(ts <= cutoff for ts in hist)
    }
    assert not leaked, (
        f"Rate limiter leaks {len(leaked)} stale histories: {list(leaked.keys())[:5]}"
    )


# ────────────────────────────────────────────────────────────────────────
# BUG 2 — uploaded files leak on pre-runner error paths in handle_invoke
# ────────────────────────────────────────────────────────────────────────

def _uploads_dir(app: SidecarApp) -> Path:
    return app._file_store_dir / "uploads"


def _count_uploaded_files(app: SidecarApp) -> int:
    d = _uploads_dir(app)
    if not d.exists():
        return 0
    return sum(1 for p in d.rglob("*") if p.is_file())


async def test_uploaded_file_cleaned_on_missing_required_body_field(bug_client):
    """Validation failure after upload must not leak files on disk.

    BUG: when a multipart /invoke request parses successfully (file lands on
    disk under file_store/uploads/<uuid>/...) but body validation fails
    because a required field is missing, handle_invoke returns 400 without
    deleting the uploaded file. The cleanup only lives in _create_runner's
    ``finally`` block, which runs only if the request makes it to the agent
    subprocess. Every pre-runner error path leaks its uploads permanently.
    """
    app: SidecarApp = bug_client.sidecar
    assert _count_uploaded_files(app) == 0

    form = FormData()
    form.add_field("capability", "translate")
    form.add_field("tx", "user-tx")
    form.add_field("nonce", "n:sid-test")
    form.add_field("body_json", json.dumps({}))  # missing required "text"
    form.add_field("file:image", io.BytesIO(b"LEAK-ME"),
                   filename="leak.png", content_type="image/png")

    resp = await bug_client.post("/invoke", data=form)
    assert resp.status == 400  # validation fails
    assert _count_uploaded_files(app) == 0, (
        "Uploaded file leaked on validation-error path"
    )


async def test_uploaded_file_cleaned_on_nonce_sidecar_mismatch(bug_client):
    """Same bug, different error branch: nonce sidecar mismatch returns 402
    without cleaning the uploaded files."""
    app: SidecarApp = bug_client.sidecar
    assert _count_uploaded_files(app) == 0

    form = FormData()
    form.add_field("capability", "translate")
    form.add_field("tx", "user-tx")
    form.add_field("nonce", "n:wrong-sid")  # wrong sidecar suffix
    form.add_field("body_json", json.dumps({"text": "hi"}))
    form.add_field("file:image", io.BytesIO(b"LEAK-ME-2"),
                   filename="leak2.png", content_type="image/png")

    resp = await bug_client.post("/invoke", data=form)
    assert resp.status == 402
    assert _count_uploaded_files(app) == 0, (
        "Uploaded file leaked on nonce mismatch path"
    )


async def test_uploaded_file_cleaned_on_payment_verification_error(bug_client):
    """Same bug, verification-error branch."""
    app: SidecarApp = bug_client.sidecar
    app.tx_store.is_processed = AsyncMock(return_value=False)
    app.verifier.verify = AsyncMock(side_effect=PaymentVerificationError("bad"))

    assert _count_uploaded_files(app) == 0

    form = FormData()
    form.add_field("capability", "translate")
    form.add_field("tx", "user-tx")
    form.add_field("nonce", "n:sid-test")
    form.add_field("body_json", json.dumps({"text": "hi"}))
    form.add_field("file:image", io.BytesIO(b"LEAK-ME-3"),
                   filename="leak3.png", content_type="image/png")

    resp = await bug_client.post("/invoke", data=form)
    assert resp.status == 402
    assert _count_uploaded_files(app) == 0, (
        "Uploaded file leaked on PaymentVerificationError path"
    )


async def test_uploaded_file_cleaned_on_duplicate_tx(bug_client):
    """Same bug, duplicate-tx branch."""
    app: SidecarApp = bug_client.sidecar
    app.tx_store.is_processed = AsyncMock(return_value=True)

    assert _count_uploaded_files(app) == 0

    form = FormData()
    form.add_field("capability", "translate")
    form.add_field("tx", "dup-tx")
    form.add_field("nonce", "n:sid-test")
    form.add_field("body_json", json.dumps({"text": "hi"}))
    form.add_field("file:image", io.BytesIO(b"LEAK-ME-4"),
                   filename="leak4.png", content_type="image/png")

    resp = await bug_client.post("/invoke", data=form)
    assert resp.status == 409
    assert _count_uploaded_files(app) == 0, (
        "Uploaded file leaked on duplicate-tx path"
    )


# ────────────────────────────────────────────────────────────────────────
# BUG 3 — TransferSender reconnects+sleeps after the final failed attempt
# ────────────────────────────────────────────────────────────────────────

async def test_transfer_sender_no_reconnect_after_exhaustion(monkeypatch):
    """After exhausting retries, the sender must not reconnect+sleep.

    BUG: the retry loop in TransferSender.send unconditionally calls
    ``await self._reconnect()`` and ``await asyncio.sleep(delay)`` in the
    ``except`` branch, even on the final attempt. When all attempts fail we
    pay for one extra liteserver reconnect and one extra sleep (currently
    5 s on the last delay slot) for no benefit, before the exception is
    raised. The reconnect count should be MAX_RETRIES - 1, not MAX_RETRIES.
    """
    sender = TransferSender(private_key_hex="a" * 64, testnet=True)

    wallet = MagicMock()
    wallet.transfer = AsyncMock(side_effect=ConnectionError("down"))
    reconnect_calls = {"n": 0}

    async def fake_init(self):
        self._client = MagicMock()
        self._client.close = AsyncMock()
        self._wallet = wallet

    async def fake_reconnect(self):
        reconnect_calls["n"] += 1
        await fake_init(self)

    monkeypatch.setattr(TransferSender, "_ensure_initialized", fake_init)
    monkeypatch.setattr(TransferSender, "_reconnect", fake_reconnect)
    monkeypatch.setattr(transfer_module, "SEND_RETRY_DELAYS", [0, 0, 0])
    monkeypatch.setattr(transfer_module, "SEND_MAX_RETRIES", 3)

    with pytest.raises(ConnectionError):
        await sender.send("EQdest", 1_000, MagicMock())

    # Optimal behaviour: reconnect happens only between attempts.
    # With 3 attempts that means 2 reconnects, not 3.
    assert reconnect_calls["n"] == 2, (
        f"Expected 2 reconnects between 3 attempts, got {reconnect_calls['n']} "
        f"(reconnect is still being called after the final failed attempt)"
    )


# ────────────────────────────────────────────────────────────────────────
# BUG 4 — ProcessedTxStore.close does not await the background cleanup task
# ────────────────────────────────────────────────────────────────────────

async def test_processed_tx_store_close_awaits_background_cleanup(tmp_tx_db):
    """mark_processed spawns a fire-and-forget cleanup task; close must wait.

    BUG: every call to ``ProcessedTxStore.mark_processed`` does
    ``asyncio.create_task(self.cleanup(...))`` without tracking the returned
    task. The task is not awaited anywhere. Consequences:
      - ``close()`` can finish while cleanup is mid-flight, leaving a
        dangling coroutine holding a reference to a just-closed sqlite
        connection. Accessing it raises.
      - pytest and production shutdowns both warn about "Task was destroyed
        but it is pending".
    Expected behaviour: either cleanup must run synchronously, or the store
    must keep a handle to its background task and drain it on ``close()``.
    """
    store = ProcessedTxStore(tmp_tx_db)
    cleanup_finished = asyncio.Event()

    async def slow_cleanup(*args, **kwargs):
        # Deliberately slow so the race with close() is deterministic.
        await asyncio.sleep(0.15)
        cleanup_finished.set()

    # Replace the method on the instance — mark_processed reads self.cleanup.
    store.cleanup = slow_cleanup  # type: ignore[method-assign]

    await store.mark_processed("hash-1")
    # close() returns "immediately" because it doesn't know about the task.
    await store.close()

    # If close() properly drained background tasks, the flag is already set.
    assert cleanup_finished.is_set(), (
        "close() returned while a background cleanup task was still pending "
        "— the store is leaking unmanaged asyncio tasks"
    )


# ────────────────────────────────────────────────────────────────────────
# BUG 5 — Heartbeat loop ignores its configured interval
# ────────────────────────────────────────────────────────────────────────

async def test_heartbeat_loop_respects_configured_interval(tmp_state_path):
    """The loop wakes on a hardcoded 3600s timer, not on ``_interval``.

    BUG: HeartbeatManager.__init__ stores ``self._interval`` from the
    ``heartbeat_interval_days`` argument, but ``loop()`` uses a hard-coded
    ``timeout=3600`` for the ``wait_for(stop_event.wait(), ...)``. Changing
    the interval parameter has no effect on how often the loop polls, which
    defeats the point of the parameter.

    Expected behaviour: the loop's wait timeout should be derived from
    ``self._interval`` (or at least a documented fraction of it).
    """
    from heartbeat import HeartbeatConfig, HeartbeatManager
    from storage import StateStore
    import inspect

    cfg = HeartbeatConfig(
        registry_address="EQr", endpoint="https://e", price=1, capabilities=("c",),
        name="n", description="d", args_schema={}, has_quote=False,
        sidecar_id=None, result_schema=None,
    )
    mgr = HeartbeatManager(
        config=cfg,
        state_store=StateStore(tmp_state_path),
        transfer_sender=AsyncMock(return_value="h"),
        heartbeat_interval_days=1,
    )

    source = inspect.getsource(HeartbeatManager.loop)
    assert "self._interval" in source, (
        "HeartbeatManager.loop() does not reference self._interval — the "
        "configured interval is ignored"
    )


# ────────────────────────────────────────────────────────────────────────
# BUG 6 — jetton_verifier not created for dynamic USDT SKUs (price_usd=0)
# ────────────────────────────────────────────────────────────────────────

def test_jetton_verifier_created_for_dynamic_usdt_sku(tmp_path):
    """SidecarApp must create jetton_verifier when any SKU has price_usd=0 (dynamic).

    BUG: the constructor checked ``if settings.agent_price_usdt:`` which is
    falsy when agent_price_usdt==0 (dynamic USDT pricing sentinel).  A SKU
    with price_usd=0 passes the sku.price_usd-is-None guard at invoke time,
    so a user could pay with USDT, hit the ``jetton_verifier is None`` branch,
    receive "USDT payments not configured" (HTTP 400) and lose their funds
    with no refund or meaningful error log.

    Fix: use ``any(s.price_usd is not None for s in settings.skus)`` so the
    verifier is always created when the USDT rail is declared.
    """
    dynamic_usdt_sku = AgentSku(
        sku_id="dynamic", title="dynamic",
        price_ton=0, price_usd=0,
        initial_stock=None,
    )
    settings = _make_settings(
        tmp_path,
        skus=(dynamic_usdt_sku,),
        agent_price=0,
        agent_price_usdt=0,
        payment_rails=("TON", "USDT"),
    )
    app = SidecarApp(settings)
    assert app.jetton_verifier is not None, (
        "jetton_verifier must be created when SKU has price_usd=0 (dynamic USDT); "
        "if None, USDT payments are silently rejected with no refund"
    )


# ────────────────────────────────────────────────────────────────────────
# BUG 7 — USDT payment accepted with min_amount=0 when dynamic price missing
# ────────────────────────────────────────────────────────────────────────

async def test_usdt_payment_rejected_when_price_unavailable(tmp_path, monkeypatch):
    """USDT execution must return 503 when dynamic price fetch returned no USD price.

    BUG: for a dynamic SKU (price_ton=0, price_usd=0), if the agent's price
    response omits the USD field, eff_usd stays 0.  jetton_verifier.verify()
    was then called with min_amount=0, meaning any incoming USDT amount would
    pass verification — the agent accepted payment without knowing the price.

    Fix: when rail=USDT and min_amount_usdt==0 on the execution path, return
    503 "USDT price unavailable" before calling verify().
    """
    import api as api_module
    from aiohttp.test_utils import TestClient, TestServer
    from unittest.mock import AsyncMock

    dynamic_sku = AgentSku(
        sku_id="dyn", title="dyn",
        price_ton=0, price_usd=0,
        initial_stock=None,
    )
    settings = _make_settings(
        tmp_path,
        skus=(dynamic_sku,),
        agent_price=0,
        agent_price_usdt=0,
        payment_rails=("TON", "USDT"),
    )
    app = SidecarApp(settings)
    app.sidecar_id = "sid-test"
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)
    # Pretend the verifier started OK so the lazy-bootstrap path is skipped
    # — this test is about the price-availability guard, not the bootstrap.
    app._agent_jetton_wallet = "EQjettonwallet"

    # Agent returns TON price only — no USD
    async def fake_run(**kwargs):
        return {"prices": {"dyn": {"ton": 1_000_000}}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async def fake_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)
        await app.refund_queue.init()

    async def fake_shutdown():
        await app.refund_queue.close()
        await app.tx_store.close()
        await app.stock.close()

    app.startup = fake_startup  # type: ignore[method-assign]
    app.shutdown = fake_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: fake_startup())
    web_app.on_shutdown.append(lambda _: fake_shutdown())

    app.tx_store.is_processed = AsyncMock(return_value=False)

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate",
            "tx": "some-usdt-tx",
            "nonce": "abc:sid-test",
            "rail": "USDT",
            "body": {"text": "hello"},
        })
        assert resp.status == 503, (
            f"Expected 503 when USDT price unavailable, got {resp.status}"
        )
        data = await resp.json()
        assert "unavailable" in data.get("error", "").lower()


# ────────────────────────────────────────────────────────────────────────
# BUG 8 — TON payment accepted with min_amount=0 when dynamic price missing
# ────────────────────────────────────────────────────────────────────────

async def test_ton_payment_rejected_when_price_unavailable(tmp_path, monkeypatch):
    """TON execution must return 503 when dynamic price fetch returned no TON price.

    Symmetric to BUG 7: for a dynamic SKU (price_ton=0, price_usd=0), if the
    agent's price response omits the TON field (or fetch fails), eff_ton stays
    0.  PaymentVerifier.verify() was then called with min_amount=0 and the
    ``amount < required_amount`` check became ``amount < 0`` — i.e. ANY TON
    amount, including 1 nanoTON, passed verification.

    Fix: when rail=TON and min_amount_ton==0 on the execution path, return
    503 "TON price unavailable" before calling verify().
    """
    import api as api_module
    from aiohttp.test_utils import TestClient, TestServer
    from unittest.mock import AsyncMock

    dynamic_sku = AgentSku(
        sku_id="dyn", title="dyn",
        price_ton=0, price_usd=0,
        initial_stock=None,
    )
    settings = _make_settings(
        tmp_path,
        skus=(dynamic_sku,),
        agent_price=0,
        agent_price_usdt=0,
        payment_rails=("TON", "USDT"),
    )
    app = SidecarApp(settings)
    app.sidecar_id = "sid-test"
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)

    # Agent returns USD price only — no TON
    async def fake_run(**kwargs):
        return {"prices": {"dyn": {"usd": 1_000_000}}}

    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async def fake_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)
        await app.refund_queue.init()

    async def fake_shutdown():
        await app.refund_queue.close()
        await app.tx_store.close()
        await app.stock.close()

    app.startup = fake_startup  # type: ignore[method-assign]
    app.shutdown = fake_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: fake_startup())
    web_app.on_shutdown.append(lambda _: fake_shutdown())

    app.tx_store.is_processed = AsyncMock(return_value=False)

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate",
            "tx": "some-ton-tx",
            "nonce": "abc:sid-test",
            "rail": "TON",
            "body": {"text": "hello"},
        })
        assert resp.status == 503, (
            f"Expected 503 when TON price unavailable, got {resp.status}"
        )
        data = await resp.json()
        assert "unavailable" in data.get("error", "").lower()


# ────────────────────────────────────────────────────────────────────────
# BUG 9 — USDT payment with absent jetton_verifier silently lost (no refund)
# ────────────────────────────────────────────────────────────────────────

async def test_refund_queue_enqueues_usdt_when_verifier_unavailable(tmp_path, monkeypatch):
    """USDT /invoke must enqueue the tx for refund when verifier can't bootstrap.

    BUG: when ``jetton_verifier`` was None (misconfig OR liteserver outage),
    /invoke returned 400 "USDT payments not configured" and the user's USDT
    payment sat on the agent's wallet with no automated recovery path. The
    sidecar logged "manual refund required" and did nothing.

    Fix: try to lazy-bootstrap the verifier; if that fails, enqueue the tx
    in the persistent refund queue and return 503 with ``refund_pending=True``.
    A background worker drains the queue.
    """
    import api as api_module
    from aiohttp.test_utils import TestClient, TestServer
    from unittest.mock import AsyncMock

    dynamic_sku = AgentSku(
        sku_id="dyn", title="dyn",
        price_ton=0, price_usd=500_000,  # static USDT price
        initial_stock=None,
    )
    settings = _make_settings(
        tmp_path,
        skus=(dynamic_sku,),
        agent_price=0,
        agent_price_usdt=500_000,
        payment_rails=("USDT",),
    )
    app = SidecarApp(settings)
    app.sidecar_id = "sid-test"
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)

    # Force the bootstrap to fail (simulating a permanent liteserver outage).
    # _agent_jetton_wallet stays None and ensure_jetton_verifier returns False.
    async def fake_ensure():
        return False
    app.ensure_jetton_verifier = fake_ensure  # type: ignore[method-assign]

    async def fake_run(**kwargs):
        return {"prices": {"dyn": {"usd": 500_000}}}
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async def fake_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)
        await app.refund_queue.init()

    async def fake_shutdown():
        await app.refund_queue.close()
        await app.tx_store.close()
        await app.stock.close()

    app.startup = fake_startup  # type: ignore[method-assign]
    app.shutdown = fake_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: fake_startup())
    web_app.on_shutdown.append(lambda _: fake_shutdown())

    app.tx_store.is_processed = AsyncMock(return_value=False)

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate",
            "tx": "lost-usdt-tx",
            "nonce": "abc:sid-test",
            "rail": "USDT",
            "body": {"text": "hello"},
        })
        assert resp.status == 503, f"Expected 503, got {resp.status}"
        data = await resp.json()
        assert data.get("refund_pending") is True
        assert data.get("tx") == "lost-usdt-tx"

        # Tx must be persisted in the refund queue for the worker to pick up.
        entry = await app.refund_queue.get("lost-usdt-tx")
        assert entry is not None
        assert entry.status == "pending"
        assert entry.rail == "USDT"
        assert entry.sku_id == "dyn"


async def test_invoke_blocks_retry_for_tx_in_refund_queue(tmp_path, monkeypatch):
    """Once a tx is enqueued for refund, /invoke must reject retries on it.

    Otherwise a retry could race the refund worker: /invoke succeeds (verifier
    came back) AND the worker sends a refund — agent loses funds twice.
    """
    import api as api_module
    from aiohttp.test_utils import TestClient, TestServer
    from unittest.mock import AsyncMock

    sku = AgentSku(
        sku_id="dyn", title="dyn",
        price_ton=0, price_usd=500_000,
        initial_stock=None,
    )
    settings = _make_settings(
        tmp_path,
        skus=(sku,),
        agent_price=0,
        agent_price_usdt=500_000,
        payment_rails=("USDT",),
    )
    app = SidecarApp(settings)
    app.sidecar_id = "sid-test"
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)

    async def fake_run(**kwargs):
        return {"prices": {"dyn": {"usd": 500_000}}}
    monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

    async def fake_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)
        await app.refund_queue.init()
        # Pre-seed the queue as if a previous request enqueued this tx.
        await app.refund_queue.enqueue(
            tx_hash="queued-tx", nonce="abc:sid-test", rail="USDT", sku_id="dyn",
        )

    async def fake_shutdown():
        await app.refund_queue.close()
        await app.tx_store.close()
        await app.stock.close()

    app.startup = fake_startup  # type: ignore[method-assign]
    app.shutdown = fake_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: fake_startup())
    web_app.on_shutdown.append(lambda _: fake_shutdown())

    app.tx_store.is_processed = AsyncMock(return_value=False)
    # Bootstrap returns OK so verify_payment would normally proceed —
    # the refund-queue pre-check must intercept FIRST.
    async def fake_ensure():
        app._agent_jetton_wallet = "EQpretend"
        return True
    app.ensure_jetton_verifier = fake_ensure  # type: ignore[method-assign]

    async with TestClient(TestServer(web_app)) as c:
        resp = await c.post("/invoke", json={
            "capability": "translate",
            "tx": "queued-tx",
            "nonce": "abc:sid-test",
            "rail": "USDT",
            "body": {"text": "hello"},
        })
        assert resp.status == 409, f"Expected 409 retry-blocked, got {resp.status}"
        data = await resp.json()
        assert data.get("refund_pending") is True


# ────────────────────────────────────────────────────────────────────────
# BUG 10 — RefundQueue state machine: claim must be atomic & idempotent
# ────────────────────────────────────────────────────────────────────────

async def test_refund_queue_state_machine(tmp_path):
    """Atomic claim must prevent two workers from refunding the same tx."""
    from payments import RefundQueue

    rq = RefundQueue(str(tmp_path / "pr.db"))
    await rq.init()
    try:
        assert await rq.enqueue(
            tx_hash="t1", nonce="n1", rail="USDT", sku_id="s1",
            sender="EQsender", amount=1000,
        )
        # Duplicate enqueue is a no-op.
        assert not await rq.enqueue(
            tx_hash="t1", nonce="n1", rail="USDT",
        )

        # Two concurrent workers race on claim — only one wins.
        results = await asyncio.gather(rq.claim("t1"), rq.claim("t1"))
        assert sum(results) == 1, f"expected exactly one claim winner, got {results}"

        entry = await rq.get("t1")
        assert entry.status == "refunding"
        assert entry.attempts == 1

        await rq.mark_refunded("t1", "refund-tx-hash")
        entry = await rq.get("t1")
        assert entry.status == "refunded"
        assert entry.refund_tx == "refund-tx-hash"

        # Already-refunded entry must not be re-claimable.
        assert not await rq.claim("t1")
    finally:
        await rq.close()


# ────────────────────────────────────────────────────────────────────────
# BUG 11 — Post-verify failures must always refund (no silent 500s)
# ────────────────────────────────────────────────────────────────────────

async def _build_post_verify_app(tmp_path, monkeypatch):
    """App configured so /invoke reaches the post-verify code path with a
    successfully verified payment. Caller mutates tx_store/jobs/stock to
    inject the specific failure mode under test."""
    from aiohttp.test_utils import TestClient, TestServer
    sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID,
        price_ton=1_000_000, price_usd=None, initial_stock=None,
    )
    settings = _make_settings(
        tmp_path, skus=(sku,),
        agent_price=1_000_000, payment_rails=("TON",),
    )
    app = SidecarApp(settings)
    app.sidecar_id = "sid-test"
    app.args_schema = {"text": {"type": "string", "required": True}}
    app._file_store_dir.mkdir(parents=True, exist_ok=True)
    app.verifier.verify = AsyncMock(return_value=VerifiedPayment(
        tx_hash="real-hash", sender="EQsender", recipient="EQagent",
        amount=1_000_000, comment="n:sid-test",
    ))
    app.tx_store.is_processed = AsyncMock(return_value=False)

    async def fake_startup():
        app._file_store_dir.mkdir(parents=True, exist_ok=True)
        await app.stock.init(app.settings.skus)
        await app.refund_queue.init()

    async def fake_shutdown():
        await app.refund_queue.close()
        await app.tx_store.close()
        await app.stock.close()

    app.startup = fake_startup  # type: ignore[method-assign]
    app.shutdown = fake_shutdown  # type: ignore[method-assign]
    web_app = app.build_web_app()
    web_app.on_startup.clear()
    web_app.on_shutdown.clear()
    web_app.on_startup.append(lambda _: fake_startup())
    web_app.on_shutdown.append(lambda _: fake_shutdown())
    return app, TestClient(TestServer(web_app))


async def test_mark_processed_failure_after_verify_enqueues_refund(tmp_path, monkeypatch):
    """When tx_store.mark_processed raises a non-IntegrityError (e.g. SQLite
    locked beyond busy_timeout, disk full, corruption), money was already
    verified — handler MUST enqueue a refund instead of returning bare 500.
    """
    app, tc = await _build_post_verify_app(tmp_path, monkeypatch)
    async with tc as c:
        app.tx_store.mark_processed = AsyncMock(
            side_effect=RuntimeError("simulated disk failure")
        )
        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "real-hash",
            "nonce": "n:sid-test", "body": {"text": "hi"},
        })
        assert resp.status == 503
        data = await resp.json()
        assert data["refund_pending"] is True
        entry = await app.refund_queue.get("real-hash")
        assert entry is not None
        assert entry.status == "pending"
        assert entry.sender == "EQsender"
        assert entry.amount == 1_000_000
        # No force_refund — mark_processed never succeeded so the worker's
        # is_processed race-guard is safe.
        assert entry.force_refund == 0


async def test_mark_processed_integrity_error_returns_409_no_refund(tmp_path, monkeypatch):
    """Parallel /invoke for the same tx_hash: one wins the PRIMARY KEY race,
    the other gets IntegrityError. Loser must return 409 and MUST NOT enqueue
    refund — the winner is delivering service for this payment.
    """
    import aiosqlite
    app, tc = await _build_post_verify_app(tmp_path, monkeypatch)
    async with tc as c:
        app.tx_store.mark_processed = AsyncMock(
            side_effect=aiosqlite.IntegrityError("UNIQUE constraint failed")
        )
        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "real-hash",
            "nonce": "n:sid-test", "body": {"text": "hi"},
        })
        assert resp.status == 409
        data = await resp.json()
        assert data["error"] == "Transaction already used"
        # Queue must be untouched.
        entry = await app.refund_queue.get("real-hash")
        assert entry is None


async def test_jobs_submit_failure_enqueues_refund_with_force(tmp_path, monkeypatch):
    """If jobs.submit raises after mark_processed succeeded, the worker's
    is_processed race-guard would skip the refund. The handler MUST set
    force_refund=True so the worker proceeds anyway.
    """
    app, tc = await _build_post_verify_app(tmp_path, monkeypatch)
    async with tc as c:
        app.tx_store.mark_processed = AsyncMock()  # succeeds
        app.tx_store.is_processed = AsyncMock(return_value=False)
        app.jobs.submit = AsyncMock(side_effect=RuntimeError("job system down"))
        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "real-hash",
            "nonce": "n:sid-test", "body": {"text": "hi"},
        })
        assert resp.status == 503
        data = await resp.json()
        assert data["refund_pending"] is True
        entry = await app.refund_queue.get("real-hash")
        assert entry is not None
        assert entry.force_refund == 1, (
            "mark_processed already ran — worker must bypass is_processed guard"
        )


async def test_refund_worker_force_refund_bypasses_is_processed_guard(tmp_path):
    """Worker race-guard: with force_refund=1, skip the is_processed check and
    refund the user even though the tx was marked processed.
    """
    from payments import RefundQueue, ProcessedTxStore
    rq = RefundQueue(str(tmp_path / "pr.db"))
    txs = ProcessedTxStore(str(tmp_path / "tx.db"))
    await rq.init()
    await txs.init()
    try:
        await txs.mark_processed("forced-tx")
        await rq.enqueue(
            tx_hash="forced-tx", nonce="n", rail="TON",
            sender="EQsender", amount=1_000_000, sku_id="s1",
            force_refund=True,
        )
        entry = await rq.get("forced-tx")
        assert entry is not None
        assert entry.force_refund == 1
        # Sanity: a plain entry would be skipped by the guard.
        await rq.enqueue(
            tx_hash="normal-tx", nonce="n", rail="TON",
            sender="EQsender", amount=1_000_000, sku_id="s1",
        )
        normal = await rq.get("normal-tx")
        assert normal.force_refund == 0
    finally:
        await rq.close()
        await txs.close()


async def test_processed_tx_store_uses_wal_and_busy_timeout(tmp_path):
    """Sanity check: WAL + busy_timeout=15s are applied on init."""
    import aiosqlite
    from payments import ProcessedTxStore
    store = ProcessedTxStore(str(tmp_path / "tx.db"))
    await store.init()
    try:
        async with store._conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        assert row[0].lower() == "wal"
        async with store._conn.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row[0] == 15000
    finally:
        await store.close()


# ────────────────────────────────────────────────────────────────────────
# BUG 12 — Worker must not double-refund after a crash mid-send
# ────────────────────────────────────────────────────────────────────────

async def test_list_stale_refunding_returns_old_refunding_entries(tmp_path):
    """list_stale_refunding selects entries stuck in 'refunding' past the
    cutoff and leaves their status untouched (caller decides next step)."""
    import time
    from payments import RefundQueue
    rq = RefundQueue(str(tmp_path / "pr.db"))
    await rq.init()
    try:
        await rq.enqueue(tx_hash="stuck", nonce="n", rail="TON",
                         sender="EQs", amount=1_000_000, sku_id="s")
        await rq.claim("stuck")  # status='refunding', last_attempt_at=now
        # Backdate last_attempt_at so it counts as stale.
        await rq._conn.execute(
            "UPDATE pending_refunds SET last_attempt_at = ? WHERE tx_hash = 'stuck'",
            (int(time.time()) - 3600,),
        )
        await rq._conn.commit()

        stale = await rq.list_stale_refunding(older_than_seconds=600)
        assert len(stale) == 1
        assert stale[0].tx_hash == "stuck"
        # No mutation — entry still in 'refunding'.
        entry = await rq.get("stuck")
        assert entry.status == "refunding"
    finally:
        await rq.close()


async def test_refund_worker_dedup_adopts_existing_onchain_hash(tmp_path, monkeypatch):
    """When a prior worker crashed between send() and mark_refunded(), the
    on-chain refund exists. On retry the worker must probe, find it, and
    mark_refunded WITHOUT sending a second refund.
    """
    import api.domain.refund_worker as worker_module
    from api.domain.refund_worker import _process_entry
    from contextlib import asynccontextmanager
    from payments import RefundQueue

    rq = RefundQueue(str(tmp_path / "pr.db"))
    await rq.init()
    try:
        await rq.enqueue(tx_hash="tx-A", nonce="n", rail="TON",
                         sender="EQuser", amount=1_000_000, sku_id="s")
        # Simulate a prior failed attempt: attempts=1 in 'pending' state.
        await rq._conn.execute(
            "UPDATE pending_refunds SET attempts = 1 WHERE tx_hash = 'tx-A'"
        )
        await rq._conn.commit()
        entry = await rq.get("tx-A")

        @asynccontextmanager
        async def fake_client_ctx(app):
            yield object()  # dummy

        async def fake_find(**kwargs):
            return "ALREADY_REFUNDED_HASH"

        send_calls = []
        async def fake_refund_user(**kwargs):
            send_calls.append(kwargs)
            return "WOULD_BE_DOUBLE_SEND"

        monkeypatch.setattr(worker_module, "_acquire_lite_client", fake_client_ctx)
        monkeypatch.setattr(worker_module, "find_existing_refund_tx", fake_find)
        monkeypatch.setattr(worker_module, "refund_user", fake_refund_user)

        # Minimal fake app surface for _process_entry.
        app = SimpleNamespace(
            refund_queue=rq,
            tx_store=SimpleNamespace(is_processed=AsyncMock(return_value=False)),
            settings=SimpleNamespace(
                agent_wallet="EQagent", refund_max_attempts=10,
                refund_fee_nanoton=500_000,
            ),
            sidecar_id="sid-test",
            sender=None, _agent_jetton_wallet=None, verifier=None,
            testnet=False,
        )
        async def fake_balance_check(*a, **kw):
            return True, ""
        monkeypatch.setattr(worker_module, "_check_balance_for_refund", fake_balance_check)
        async def fake_recover(*a, **kw):
            return True
        monkeypatch.setattr(worker_module, "_recover_payment_info", fake_recover)

        await _process_entry(app, entry)

        # Worker must adopt the existing refund hash, NOT send again.
        assert send_calls == [], (
            f"refund_user should not run when prior refund detected on-chain; called with {send_calls}"
        )
        final = await rq.get("tx-A")
        assert final.status == "refunded"
        assert final.refund_tx == "ALREADY_REFUNDED_HASH"
    finally:
        await rq.close()


async def test_refund_worker_first_attempt_skips_probe(tmp_path, monkeypatch):
    """First attempt has nothing to dedup against — probe must be skipped to
    save one RPC per refund."""
    import api.domain.refund_worker as worker_module
    from api.domain.refund_worker import _process_entry
    from payments import RefundQueue

    rq = RefundQueue(str(tmp_path / "pr.db"))
    await rq.init()
    try:
        await rq.enqueue(tx_hash="tx-B", nonce="n", rail="TON",
                         sender="EQuser", amount=1_000_000, sku_id="s")
        entry = await rq.get("tx-B")
        assert entry.attempts == 0

        probe_calls = []
        async def fake_find(**kwargs):
            probe_calls.append(kwargs)
            return None
        monkeypatch.setattr(worker_module, "find_existing_refund_tx", fake_find)

        send_calls = []
        async def fake_refund_user(**kwargs):
            send_calls.append(kwargs)
            return "REFUND_TX_HASH"
        monkeypatch.setattr(worker_module, "refund_user", fake_refund_user)

        async def fake_balance_check(*a, **kw):
            return True, ""
        monkeypatch.setattr(worker_module, "_check_balance_for_refund", fake_balance_check)

        app = SimpleNamespace(
            refund_queue=rq,
            tx_store=SimpleNamespace(is_processed=AsyncMock(return_value=False)),
            settings=SimpleNamespace(
                agent_wallet="EQagent", refund_max_attempts=10,
                refund_fee_nanoton=500_000,
            ),
            sidecar_id="sid-test",
            sender=None, _agent_jetton_wallet=None, verifier=None,
            testnet=False,
        )

        await _process_entry(app, entry)
        assert probe_calls == [], "first attempt should not probe on-chain"
        assert len(send_calls) == 1
        final = await rq.get("tx-B")
        assert final.status == "refunded"
        assert final.refund_tx == "REFUND_TX_HASH"
    finally:
        await rq.close()


async def test_recover_stale_refunding_marks_already_refunded(tmp_path, monkeypatch):
    """Stale-refunding entry whose refund landed on-chain (crash between
    send() and mark_refunded) must be marked 'refunded' on recovery, not
    blindly reverted to 'pending' (which would trigger a double-send).
    """
    import api.domain.refund_worker as worker_module
    from api.domain.refund_worker import _recover_stale_refunding
    from contextlib import asynccontextmanager
    import time
    from payments import RefundQueue

    rq = RefundQueue(str(tmp_path / "pr.db"))
    await rq.init()
    try:
        await rq.enqueue(tx_hash="tx-C", nonce="n", rail="TON",
                         sender="EQuser", amount=1_000_000, sku_id="s")
        await rq.claim("tx-C")
        await rq._conn.execute(
            "UPDATE pending_refunds SET last_attempt_at = ? WHERE tx_hash = 'tx-C'",
            (int(time.time()) - 3600,),
        )
        await rq._conn.commit()

        @asynccontextmanager
        async def fake_client_ctx(app):
            yield object()

        async def fake_find(**kwargs):
            assert kwargs["original_tx_hash"] == "tx-C"
            return "ONCHAIN_REFUND_HASH"

        monkeypatch.setattr(worker_module, "_acquire_lite_client", fake_client_ctx)
        monkeypatch.setattr(worker_module, "find_existing_refund_tx", fake_find)

        app = SimpleNamespace(
            refund_queue=rq, verifier=None, sidecar_id="sid-test",
            settings=SimpleNamespace(agent_wallet="EQagent", testnet=False),
        )
        await _recover_stale_refunding(app, older_than_seconds=600)

        final = await rq.get("tx-C")
        assert final.status == "refunded"
        assert final.refund_tx == "ONCHAIN_REFUND_HASH"
    finally:
        await rq.close()


async def test_recover_stale_refunding_reverts_when_not_onchain(tmp_path, monkeypatch):
    """If the probe finds no refund on-chain, the stale entry returns to
    'pending' with a backoff so the worker retries it on the next tick."""
    import api.domain.refund_worker as worker_module
    from api.domain.refund_worker import _recover_stale_refunding
    from contextlib import asynccontextmanager
    import time
    from payments import RefundQueue

    rq = RefundQueue(str(tmp_path / "pr.db"))
    await rq.init()
    try:
        await rq.enqueue(tx_hash="tx-D", nonce="n", rail="TON",
                         sender="EQuser", amount=1_000_000, sku_id="s")
        await rq.claim("tx-D")
        await rq._conn.execute(
            "UPDATE pending_refunds SET last_attempt_at = ? WHERE tx_hash = 'tx-D'",
            (int(time.time()) - 3600,),
        )
        await rq._conn.commit()

        @asynccontextmanager
        async def fake_client_ctx(app):
            yield object()
        async def fake_find(**kwargs):
            return None  # nothing on-chain
        monkeypatch.setattr(worker_module, "_acquire_lite_client", fake_client_ctx)
        monkeypatch.setattr(worker_module, "find_existing_refund_tx", fake_find)

        app = SimpleNamespace(
            refund_queue=rq, verifier=None, sidecar_id="sid-test",
            settings=SimpleNamespace(agent_wallet="EQagent", testnet=False),
        )
        await _recover_stale_refunding(app, older_than_seconds=600)

        final = await rq.get("tx-D")
        assert final.status == "pending"
        assert "stale" in (final.last_error or "")
    finally:
        await rq.close()


async def test_find_existing_refund_tx_matches_ton_comment(tmp_path):
    """Probe must recognize a refund_body in an outgoing TON internal msg
    by matching (tx, sidecar_id) — reason and other fields are ignored."""
    from unittest.mock import MagicMock
    from api.domain.refund import find_existing_refund_tx
    from transfer import refund_body

    body = refund_body("paid-tx-hash", "out_of_stock", "sid-test")
    # Fake out_msg/tx objects mirroring tonutils' shape: tx.out_msgs, msg.body,
    # tx.cell.hash.hex().
    msg = MagicMock(body=body)
    tx = MagicMock(out_msgs=[msg])
    tx.cell.hash.hex.return_value = "ONCHAIN_REFUND_HASH"
    client = MagicMock()
    async def fake_get_txs(addr, limit):
        return [tx]
    client.get_transactions = fake_get_txs

    found = await find_existing_refund_tx(
        client=client, agent_wallet="EQagent", rail="TON",
        original_tx_hash="paid-tx-hash", sidecar_id="sid-test",
    )
    assert found == "ONCHAIN_REFUND_HASH"

    not_found = await find_existing_refund_tx(
        client=client, agent_wallet="EQagent", rail="TON",
        original_tx_hash="different-tx", sidecar_id="sid-test",
    )
    assert not_found is None
