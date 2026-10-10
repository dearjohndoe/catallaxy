"""Solana registry heartbeat + Solana-only agent (no TON key)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from chains.solana.constants import (
    MEMO_PROGRAM,
    REG_HEARTBEAT_LAMPORTS,
    REG_MEMO_PREFIX,
    REGISTRY_ADDRESS_DEVNET,
)
from chains.solana.registry import (
    MAX_TX_BYTES,
    SolanaRegistry,
    parse_reg_memo,
    read_registry,
    reg_memo,
    split_rendered_memos,
)
from chains.ton.heartbeat import MAX_PAYLOAD_BYTES, HeartbeatConfig, HeartbeatManager
from settings import AgentSku, DEFAULT_SKU_ID
from storage import SidecarState, StateStore

from tests.test_api import app_factory, _serve_app  # noqa: F401 (app_factory is a fixture)

REGISTRY = REGISTRY_ADDRESS_DEVNET


def _rendered(*memos: str) -> str:
    return "; ".join(f"[{len(m.encode())}] {m}" for m in memos)


class FakeRpc:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.sent: list[str] = []
        self.statuses: list[dict[str, Any] | None] = [{"confirmationStatus": "confirmed", "err": None}]
        self.closed = False

    async def get_signatures_for_address(self, address, *, limit=100, before=None, until=None,
                                          commitment="confirmed"):
        assert address == REGISTRY
        return self.rows[:limit]

    async def get_latest_blockhash(self):
        return "11111111111111111111111111111111", 100

    async def send_transaction(self, tx_b64):
        self.sent.append(tx_b64)
        return f"sig{len(self.sent)}"

    async def signature_status(self, sig):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    async def close(self):
        self.closed = True


def _registry(rpc: FakeRpc, kp: Keypair | None = None) -> SolanaRegistry:
    reg = SolanaRegistry(keypair=kp or Keypair(), rpc=rpc, registry_address=REGISTRY)  # type: ignore[arg-type]
    reg.CONFIRM_TIMEOUT = 0.05
    return reg


def _decode(b64: str) -> VersionedTransaction:
    return VersionedTransaction.from_bytes(base64.b64decode(b64))


def _payload(**kw: Any) -> dict[str, Any]:
    base = {"name": "A", "endpoint": "https://a.test", "rails": ["SOL"], "sidecar_id": "sid-1"}
    base.update(kw)
    return base


# ── memo parsing ────────────────────────────────────────────────────────

def test_split_rendered_memos_uses_byte_length():
    tricky = 'CTLX:REG:{"name":"a; [5] b","x":"ünï"}'
    assert split_rendered_memos(_rendered(tricky, "other")) == [tricky, "other"]
    assert split_rendered_memos(None) == []
    assert split_rendered_memos("raw text") == ["raw text"]


def test_parse_reg_memo_picks_reg_memo_and_skips_garbage():
    good = reg_memo(_payload())
    assert parse_reg_memo(_rendered("CTLX:PAY:x", good)) == _payload()
    assert parse_reg_memo(_rendered(REG_MEMO_PREFIX + "{not json")) is None
    assert parse_reg_memo(_rendered(REG_MEMO_PREFIX + "[1,2]")) is None
    assert parse_reg_memo(_rendered("hello")) is None


async def test_read_registry_latest_heartbeat_wins_and_skips_failed():
    rpc = FakeRpc()
    # newest first, as the RPC returns them
    rpc.rows = [
        {"signature": "s4", "err": {"x": 1}, "memo": _rendered(reg_memo(_payload(name="failed"))), "blockTime": 4},
        {"signature": "s3", "err": None, "memo": _rendered(reg_memo(_payload(name="new"))), "blockTime": 3},
        {"signature": "s2", "err": None, "memo": None, "blockTime": 2},
        {"signature": "s1", "err": None, "memo": _rendered(reg_memo(_payload(name="old"))), "blockTime": 1},
        {"signature": "s0", "err": None, "memo": _rendered(reg_memo({"name": "no id"})), "blockTime": 0},
    ]
    listings = await read_registry(rpc, REGISTRY)  # type: ignore[arg-type]
    assert list(listings) == ["sid-1"]
    assert listings["sid-1"]["payload"]["name"] == "new"
    assert listings["sid-1"]["signature"] == "s3"


# ── publish ─────────────────────────────────────────────────────────────

async def test_publish_heartbeat_sends_transfer_and_reg_memo():
    rpc = FakeRpc()
    kp = Keypair()
    sig = await _registry(rpc, kp).publish_heartbeat(_payload())
    assert sig == "sig1"
    tx = _decode(rpc.sent[0])
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    assert keys[0] == str(kp.pubkey())  # seller pays fee and signs
    assert REGISTRY in keys
    memo_ix = next(ix for ix in msg.instructions if keys[ix.program_id_index] == str(MEMO_PROGRAM))
    assert bytes(memo_ix.data).decode() == reg_memo(_payload())
    transfer_ix = next(ix for ix in msg.instructions if ix is not memo_ix)
    assert int.from_bytes(bytes(transfer_ix.data)[4:12], "little") == REG_HEARTBEAT_LAMPORTS
    assert keys[transfer_ix.accounts[1]] == REGISTRY


async def test_max_size_payload_fits_in_one_tx():
    """The 900-byte descriptor cap must fit the 1232-byte tx (with headroom)."""
    pad = "x" * (MAX_PAYLOAD_BYTES - len(json.dumps(_payload(name=""), ensure_ascii=False)))
    payload = _payload(name=pad)
    assert len(json.dumps(payload, ensure_ascii=False).encode()) == MAX_PAYLOAD_BYTES
    tx = _registry(FakeRpc()).build_tx(payload, "11111111111111111111111111111111")
    assert len(bytes(tx)) <= MAX_TX_BYTES


async def test_publish_oversized_payload_raises_without_sending():
    rpc = FakeRpc()
    with pytest.raises(ValueError, match="bytes"):
        await _registry(rpc).publish_heartbeat(_payload(name="x" * 1200))
    assert rpc.sent == []


async def test_publish_waits_for_confirmation():
    rpc = FakeRpc()
    rpc.statuses = [None, {"confirmationStatus": "processed", "err": None},
                    {"confirmationStatus": "confirmed", "err": None}]
    reg = _registry(rpc)
    reg.CONFIRM_TIMEOUT = 10

    async def no_sleep(_):
        return None

    orig = asyncio.sleep
    asyncio.sleep = no_sleep  # type: ignore[assignment]
    try:
        assert await reg.publish_heartbeat(_payload()) == "sig1"
    finally:
        asyncio.sleep = orig  # type: ignore[assignment]
    assert rpc.statuses == [{"confirmationStatus": "confirmed", "err": None}]


async def test_publish_failed_or_unconfirmed_tx_raises():
    rpc = FakeRpc()
    rpc.statuses = [{"confirmationStatus": "confirmed", "err": {"InstructionError": [0, "x"]}}]
    with pytest.raises(RuntimeError, match="failed"):
        await _registry(rpc).publish_heartbeat(_payload())
    rpc.statuses = [None]
    with pytest.raises(TimeoutError):
        await _registry(rpc).publish_heartbeat(_payload())


# ── HeartbeatManager over the Solana registry ───────────────────────────

def _config(**kw: Any) -> HeartbeatConfig:
    base = dict(
        registry_address="EQregistry", endpoint="https://a.test", price=0,
        capabilities=("translate",), name="A", description="d", args_schema={},
        sidecar_id="sid-1", rails=("SOL", "USDC"),
    )
    base.update(kw)
    return HeartbeatConfig(**base)


def test_payload_carries_solana_prices():
    mgr = HeartbeatManager(_config(price_sol=2_000_000, price_usdc=1_500_000),
                           StateStore("/nonexistent"), registry=object())
    payload = mgr._build_payload()
    assert payload["rails"] == ["SOL", "USDC"]
    assert (payload["price_sol"], payload["price_usdc"]) == (2_000_000, 1_500_000)
    no_prices = HeartbeatManager(_config(), StateStore("/nonexistent"), registry=object())._build_payload()
    assert "price_sol" not in no_prices and "price_usdc" not in no_prices


def test_manager_needs_registry_or_ton_sender():
    with pytest.raises(ValueError):
        HeartbeatManager(_config(), StateStore("/nonexistent"))


async def test_solana_manager_uses_own_timestamp(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    store.save(SidecarState(last_heartbeat="2020-01-01T00:00:00Z", sidecar_id="sid-1"))
    rpc = FakeRpc()
    mgr = HeartbeatManager(_config(), store, registry=_registry(rpc), state_field="last_sol_heartbeat")
    assert mgr.chain == "solana"
    assert await mgr.send_if_needed() is True
    state = store.load()
    assert state.last_heartbeat == "2020-01-01T00:00:00Z"  # TON timestamp untouched
    assert state.last_sol_heartbeat and state.last_sol_heartbeat.endswith("Z")
    assert await mgr.send_if_needed() is False  # fresh now
    assert len(rpc.sent) == 1


async def test_concurrent_managers_keep_both_timestamps(tmp_path):
    """A slow TON send must not overwrite the Solana timestamp saved meanwhile."""
    store = StateStore(str(tmp_path / "state.json"))
    store.save(SidecarState(sidecar_id="sid-1"))
    release = asyncio.Event()

    async def slow_ton_send(dest, amount, body):
        await release.wait()
        return "ton-hash"

    ton = HeartbeatManager(_config(), store, transfer_sender=slow_ton_send)
    sol = HeartbeatManager(_config(), store, registry=_registry(FakeRpc()), state_field="last_sol_heartbeat")
    ton_task = asyncio.create_task(ton.send_if_needed())
    await asyncio.sleep(0)
    assert await sol.send_if_needed() is True
    release.set()
    assert await ton_task is True
    state = store.load()
    assert state.last_heartbeat and state.last_sol_heartbeat


def test_state_file_without_sol_timestamp_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_heartbeat": None, "sidecar_id": "sid-1"}))
    assert StateStore(str(path)).load().last_sol_heartbeat is None


# ── settings: Solana-only agent ─────────────────────────────────────────

def _base_env(monkeypatch, *, ton: bool = False, sol: bool = True) -> Keypair:
    for k, v in {
        "AGENT_COMMAND": "true", "AGENT_CAPABILITY": "x", "AGENT_NAME": "A",
        "AGENT_DESCRIPTION": "d", "AGENT_ENDPOINT": "https://a.test",
    }.items():
        monkeypatch.setenv(k, v)
    kp = Keypair()
    if ton:
        monkeypatch.setenv("TON_WALLET_PK", "a" * 64)
    if sol:
        monkeypatch.setenv("SOL_WALLET_PK", json.dumps(list(bytes(kp))))
    return kp


def test_settings_solana_only_agent_needs_no_ton_key(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    monkeypatch.setenv("SOL_CLUSTER", "devnet")
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:sol=2000000:usdc=1500000")
    s = load_settings(env_file="/nonexistent")
    assert (s.agent_wallet_pk, s.agent_wallet) == (None, None)
    assert s.payment_rails == ("USDC", "SOL")
    assert s.sol_registry_address == REGISTRY_ADDRESS_DEVNET


def test_settings_ton_rail_still_requires_ton_key(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:ton=1:sol=2")
    with pytest.raises(RuntimeError, match="TON_WALLET_PK.*TON rail"):
        load_settings(env_file="/nonexistent")


def test_settings_needs_some_chain_key(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch, sol=False)
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:free")
    with pytest.raises(RuntimeError, match="TON_WALLET_PK"):
        load_settings(env_file="/nonexistent")
    monkeypatch.setenv("SOL_WALLET_PK", json.dumps(list(bytes(Keypair()))))
    assert load_settings(env_file="/nonexistent").payment_rails == ()


def test_settings_sol_registry_address(clean_env, monkeypatch):
    from settings import load_settings
    _base_env(monkeypatch)
    monkeypatch.setenv("AGENT_SKUS", "basic:inf:sol=10")
    assert load_settings(env_file="/nonexistent").sol_registry_address is None  # mainnet: no default
    other = str(Keypair().pubkey())
    monkeypatch.setenv("SOL_REGISTRY_ADDRESS", other)
    assert load_settings(env_file="/nonexistent").sol_registry_address == other
    monkeypatch.setenv("SOL_REGISTRY_ADDRESS", "not-an-address")
    with pytest.raises(RuntimeError, match="SOL_REGISTRY_ADDRESS"):
        load_settings(env_file="/nonexistent")


# ── app wiring ──────────────────────────────────────────────────────────

def _sol_only_app(app_factory, **kw: Any):
    sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID,
        price_ton=None, price_usd=None, initial_stock=None, price_sol=2_000_000,
    )
    base = dict(
        agent_price=0, agent_wallet_pk=None, agent_wallet=None,
        skus=(sku,), payment_rails=("SOL",), agent_price_sol=2_000_000,
        sol_wallet_pk=json.dumps(list(bytes(Keypair()))), sol_cluster="devnet",
        sol_rpc_url="http://rpc.invalid", sol_registry_address=REGISTRY,
    )
    base.update(kw)
    return app_factory(**base)


def test_solana_only_app_has_no_ton_components(app_factory):
    app = _sol_only_app(app_factory)
    assert (app.verifier, app.sender) == (None, None)
    assert set(app.rails) == {"SOL"}


def test_usdc_health_comes_from_sol_poller_when_both_rails_run(app_factory):
    both = _sol_only_app(app_factory, payment_rails=("SOL", "USDC"))
    usdc, sol = both.rails["USDC"], both.rails["SOL"]
    assert usdc.health_from == sol.monitor_healthy
    usdc_only = _sol_only_app(app_factory, payment_rails=("USDC",))
    assert usdc_only.rails["USDC"].health_from is None


async def test_build_heartbeats_per_chain_key(app_factory):
    from api.lifecycle import build_heartbeats

    sol_only = build_heartbeats(_sol_only_app(app_factory))
    assert [hb.chain for hb in sol_only] == ["solana"]
    assert sol_only[0]._build_payload()["price_sol"] == 2_000_000
    await sol_only[0].registry.close()

    no_registry = build_heartbeats(_sol_only_app(app_factory, sol_registry_address=None))
    assert no_registry == []

    ton_and_sol = build_heartbeats(_sol_only_app(
        app_factory, agent_wallet_pk="a" * 64, agent_wallet="EQagent",
    ))
    assert [hb.chain for hb in ton_and_sol] == ["ton", "solana"]
    await ton_and_sol[1].registry.close()

    ton_only = build_heartbeats(app_factory())
    assert [hb.chain for hb in ton_only] == ["ton"]


async def test_solana_only_shutdown_skips_ton(app_factory):
    from api.lifecycle import build_heartbeats, shutdown

    app = _sol_only_app(app_factory)
    app.heartbeats = build_heartbeats(app)
    reg_rpc = app.heartbeats[0].registry._rpc
    closed = []

    async def close():
        closed.append(True)

    reg_rpc.close = close
    await shutdown(app)
    assert closed == [True]


async def test_solana_only_invoke_defaults_to_sol_and_rejects_ton(app_factory):
    app = _sol_only_app(app_factory)
    app.rails["SOL"].monitor_healthy = lambda max_age_seconds=60.0: True
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={"capability": "translate", "body": {"text": "hi"}})
        assert resp.status == 402, await resp.text()
        data = await resp.json()
        assert [o["rail"] for o in data["payment_options"]] == ["SOL"]
        assert "x-ton-pay-address" not in resp.headers

        ton = await c.post("/invoke", json={
            "capability": "translate", "rail": "TON", "body": {"text": "hi"},
        })
        assert ton.status == 400
        assert (await ton.json())["error"] == "unsupported_rail_for_sku"

        info = await (await c.get("/info")).json()
        assert info["payment_rails"] == ["SOL"]
        assert info["skus"][0]["price_sol"] == 2_000_000
        assert "price_ton" not in info["skus"][0]


async def test_multipart_invoke_reads_x402_header_and_default_rail(app_factory):
    """The web frontend posts multipart: an x402 header there must reach the
    USDC path, and a missing ``rail`` field must not force TON."""
    from aiohttp import FormData

    sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID, price_ton=None, price_usd=None,
        initial_stock=None, price_sol=2_000_000, price_usdc=10_000,
    )
    app = _sol_only_app(app_factory, skus=(sku,), payment_rails=("SOL", "USDC"), agent_price_usdc=10_000)
    app.rails["SOL"].monitor_healthy = lambda max_age_seconds=60.0: True
    app.rails["USDC"].monitor_healthy = lambda max_age_seconds=60.0: True

    def form() -> FormData:
        f = FormData()
        f.add_field("capability", "translate")
        f.add_field("body_json", json.dumps({"text": "hi"}), content_type="application/json")  # forces multipart
        return f

    async with _serve_app(app) as c:
        pre = await c.post("/invoke", data=form())
        assert pre.status == 402, await pre.text()
        assert {o["rail"] for o in (await pre.json())["payment_options"]} == {"SOL", "USDC"}

        bad = await c.post("/invoke", data=form(), headers={"PAYMENT-SIGNATURE": "e30="})
        assert bad.status == 402
        assert "x402" in (await bad.json())["error"]  # inspected as x402, not "nonce is required"


async def test_usdc_settle_throttle_answers_429_with_retry_after(app_factory):
    """A throttled settle never reached the chain: 429 + Retry-After, the client
    IP goes to the settler, and the payload is not marked processed."""
    from types import SimpleNamespace

    from chains.solana.exact import SettleThrottled

    sku = AgentSku(
        sku_id=DEFAULT_SKU_ID, title=DEFAULT_SKU_ID, price_ton=None, price_usd=None,
        initial_stock=None, price_sol=None, price_usdc=10_000,
    )
    app = _sol_only_app(app_factory, skus=(sku,), payment_rails=("USDC",), agent_price_usdc=10_000)
    app.rails["USDC"].monitor_healthy = lambda max_age_seconds=60.0: True
    app.rails["USDC"].inspect = lambda proof, min_amount: SimpleNamespace(
        identity="solana:clientsig", client_signature="clientsig",
    )
    seen: list = []

    async def throttled(inspected, *, client_key=None):
        seen.append(client_key)
        raise SettleThrottled("settle_in_progress", 5)

    app.rails["USDC"].settle = throttled
    async with _serve_app(app) as c:
        resp = await c.post(
            "/invoke", json={"capability": "translate", "rail": "USDC", "body": {"text": "hi"}},
            headers={"PAYMENT-SIGNATURE": "e30="},
        )
        assert resp.status == 429, await resp.text()
        assert resp.headers["Retry-After"] == "5"
        assert await resp.json() == {"error": "settle_in_progress", "retry_after_seconds": 5}
        assert not await app.tx_store.is_processed("solana:clientsig")
    assert seen and seen[0]
