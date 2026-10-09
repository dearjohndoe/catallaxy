"""Tests for split-nonce claim authentication (TODO-claim-auth.md).

The claim nonce (`POST /invoke {tx, nonce}`) splits into `pub(8 hex)` — the
on-chain correlator, public by construction — and `sec(8 hex)` — a
capability secret that only ever appears in the 402 response body. A claim
must present the correct `sec` for the `pub` the sidecar minted, or it's
rejected with 403 *before* touching the chain, `tx_store`, or the refund
queue. This closes the front-running gap where `{tx, pub}` alone (everything
visible on-chain) used to be sufficient to steal a real payer's claim.

Uses the same settings/app-serving helpers as test_api.py (imported directly
— they're plain functions/fixtures, not special to that module).
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
from unittest.mock import AsyncMock

from payments import PaymentVerificationError, VerifiedPayment
from payments.nonce import mint_nonce, split_full_nonce

from tests.test_api import app_factory, _serve_app, _force_healthy_monitors  # noqa: F401 (app_factory is a fixture)


# ── Helpers ──────────────────────────────────────────────────────────────

def _mint_and_seed(app, ttl: int = 3600) -> tuple[str, str, str]:
    """Mimic build_402_response: mint pub/sec/full and register the secret's
    hash. Returns (pub, sec, full_nonce)."""
    pub, sec, _pub_nonce, full_nonce = mint_nonce(app.sidecar_id)
    return pub, sec, full_nonce


async def _seed(app, pub: str, sec: str, ttl_seconds: int = 3600) -> None:
    await app.claim_secrets.insert(
        pub, hashlib.sha256(sec.encode()).hexdigest(), int(time.time()) + ttl_seconds,
    )


def _verified(tx_hash: str = "real-hash") -> VerifiedPayment:
    return VerifiedPayment(
        tx_hash=tx_hash, sender="EQsender", recipient="EQagent",
        amount=1_000_000, comment="",
    )


# ── Front-running rejected ──────────────────────────────────────────────

async def test_frontrun_wrong_secret_rejected_no_side_effects(app_factory, tmp_path, monkeypatch):
    """Attacker knows pub (it's on-chain) but guesses the wrong sec.

    Must get 403, and must NOT mark the tx processed, consume the nonce, or
    enqueue a refund — the real payer's later claim (correct pub+sec) must
    still succeed."""
    import api as api_module

    app = app_factory()
    async with _serve_app(app) as c:
        pub, sec, full_nonce = _mint_and_seed(app)
        await _seed(app, pub, sec)
        app.verifier.verify = AsyncMock(return_value=_verified())
        app.tx_store.mark_processed = AsyncMock()

        wrong_full_nonce = f"{pub}deadbeef:{app.sidecar_id}"
        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "some-tx", "nonce": wrong_full_nonce,
            "body": {"text": "hi"},
        })
        assert resp.status == 403
        data = await resp.json()
        assert "claim" in data["error"].lower()

        # No side effects from the failed guess.
        app.verifier.verify.assert_not_called()
        app.tx_store.mark_processed.assert_not_called()
        assert await app.refund_queue.get("ton:some-tx") is None
        assert await app.refund_queue.get(f"ton:pub:{pub}") is None

        # Real payer retries with the correct secret and succeeds.
        async def fake_run(**kwargs):
            return {"result": {"type": "text", "data": "ok"}}

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        resp2 = await c.post("/invoke", json={
            "capability": "translate", "tx": "some-tx", "nonce": full_nonce,
            "body": {"text": "hi"},
        })
        assert resp2.status == 200
        assert (await resp2.json())["status"] == "done"
        app.verifier.verify.assert_awaited_once()
        marked = [c.args[0] for c in app.tx_store.mark_processed.await_args_list]
        assert "ton:real-hash" in marked
        assert f"ton:pub:{pub}" in marked


async def test_frontrun_missing_secret_rejected(app_factory, tmp_path):
    """No claim_secrets row at all for `pub` (e.g. attacker fabricated a
    plausible-looking nonce) — must 403, same as a wrong secret."""
    app = app_factory()
    async with _serve_app(app) as c:
        app.verifier.verify = AsyncMock(return_value=_verified())
        fabricated = "aaaaaaaabbbbbbbb:" + app.sidecar_id
        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "ghost-tx", "nonce": fabricated,
            "body": {"text": "hi"},
        })
        assert resp.status == 403
        app.verifier.verify.assert_not_called()
        assert await app.tx_store.is_processed("ton:ghost-tx") is False


async def test_frontrun_malformed_nonce_rejected_cleanly(app_factory, tmp_path):
    """A nonce that isn't even the right shape (not 16 hex + suffix) must be
    rejected with a clean 403, never an unhandled-exception 500."""
    app = app_factory()
    async with _serve_app(app) as c:
        app.verifier.verify = AsyncMock(return_value=_verified())
        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "tx1", "nonce": "short:" + app.sidecar_id,
            "body": {"text": "hi"},
        })
        assert resp.status == 403
        app.verifier.verify.assert_not_called()


# ── Happy path ───────────────────────────────────────────────────────────

async def test_happy_path_correct_secret_delivers_once(app_factory, tmp_path, monkeypatch):
    """Correct pub+sec ⇒ delivery, tx marked processed exactly once."""
    import api as api_module

    app = app_factory()
    async with _serve_app(app) as c:
        pub, sec, full_nonce = _mint_and_seed(app)
        await _seed(app, pub, sec)
        app.tx_store.is_processed = AsyncMock(return_value=False)
        app.tx_store.mark_processed = AsyncMock()
        app.verifier.verify = AsyncMock(return_value=_verified())

        async def fake_run(**kwargs):
            return {"result": {"type": "text", "data": "ok"}}

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "user-tx", "nonce": full_nonce,
            "body": {"text": "hi"},
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "done"
        marked = [c.args[0] for c in app.tx_store.mark_processed.await_args_list]
        assert "ton:real-hash" in marked
        assert f"ton:pub:{pub}" in marked

        # The verifier was called with the *public-only* half, not the full
        # nonce (sec never reaches the chain lookup).
        _, kwargs = app.verifier.verify.call_args
        assert kwargs["raw_nonce"] == f"{pub}:{app.sidecar_id}"

        # Post-success hygiene: the claim secret row is gone (best-effort
        # delete), though this is NOT required for correctness.
        assert await app.claim_secrets.check(pub) is None


# ── Expired claim secret ─────────────────────────────────────────────────

async def test_expired_claim_secret_rejected(app_factory, tmp_path):
    app = app_factory()
    async with _serve_app(app) as c:
        pub, sec, full_nonce = _mint_and_seed(app)
        # Seed already-expired (expires_at in the past).
        await app.claim_secrets.insert(
            pub, hashlib.sha256(sec.encode()).hexdigest(), int(time.time()) - 1,
        )
        app.verifier.verify = AsyncMock(return_value=_verified())

        resp = await c.post("/invoke", json={
            "capability": "translate", "tx": "tx-exp", "nonce": full_nonce,
            "body": {"text": "hi"},
        })
        assert resp.status == 403
        app.verifier.verify.assert_not_called()
        assert await app.tx_store.is_processed("ton:tx-exp") is False


# ── Concurrent double-claim, same correct secret ─────────────────────────

async def test_concurrent_double_claim_same_secret_one_wins_409(app_factory, tmp_path, monkeypatch):
    """Two concurrent claims with the identical correct secret: both pass the
    secret check (it's non-destructive), both race at mark_processed —
    exactly one wins, the other gets the existing 409 IntegrityError path,
    not a new secret-related error."""
    import api as api_module

    app = app_factory()
    async with _serve_app(app) as c:
        pub, sec, full_nonce = _mint_and_seed(app)
        await _seed(app, pub, sec)
        app.verifier.verify = AsyncMock(return_value=_verified(tx_hash="race-hash"))

        async def fake_run(**kwargs):
            return {"result": {"type": "text", "data": "ok"}}

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        payload = {
            "capability": "translate", "tx": "race-tx", "nonce": full_nonce,
            "body": {"text": "hi"},
        }
        r1, r2 = await asyncio.gather(
            c.post("/invoke", json=payload),
            c.post("/invoke", json=payload),
        )
        statuses = sorted([r1.status, r2.status])
        assert statuses == [200, 409], f"expected one 200 + one 409, got {statuses}"
        loser = r1 if r1.status == 409 else r2
        loser_data = await loser.json()
        assert loser_data["error"] == "Transaction already used"


# ── Retry after verify_payment timeout ───────────────────────────────────

async def test_retry_after_verify_timeout_succeeds_with_same_secret(app_factory, tmp_path, monkeypatch):
    """verify_payment fails/times out on attempt 1 with no side effects; the
    secret must still be valid so attempt 2 with the SAME nonce succeeds once
    the tx is visible on-chain. This is the regression test for the
    destructive-delete-on-read design that was considered and rejected
    (see TODO-claim-auth.md 'Mechanism' / 'Claim path')."""
    import api as api_module

    app = app_factory()
    async with _serve_app(app) as c:
        pub, sec, full_nonce = _mint_and_seed(app)
        await _seed(app, pub, sec)

        app.verifier.verify = AsyncMock(
            side_effect=PaymentVerificationError("Transaction not found")
        )

        resp1 = await c.post("/invoke", json={
            "capability": "translate", "tx": "slow-tx", "nonce": full_nonce,
            "body": {"text": "hi"},
        })
        assert resp1.status == 402
        data1 = await resp1.json()
        assert data1["error"] == "Transaction not found"
        assert await app.tx_store.is_processed("ton:slow-tx") is False

        # The secret must still be valid (not consumed by the failed attempt).
        assert await app.claim_secrets.check(pub) is not None

        # Attempt 2: tx now visible on-chain, same nonce.
        app.verifier.verify = AsyncMock(return_value=_verified(tx_hash="slow-hash"))
        app.tx_store.mark_processed = AsyncMock()

        async def fake_run(**kwargs):
            return {"result": {"type": "text", "data": "ok"}}

        monkeypatch.setattr(api_module, "run_agent_subprocess", fake_run)

        resp2 = await c.post("/invoke", json={
            "capability": "translate", "tx": "slow-tx", "nonce": full_nonce,
            "body": {"text": "hi"},
        })
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["status"] == "done"
        marked = [c.args[0] for c in app.tx_store.mark_processed.await_args_list]
        assert "ton:slow-hash" in marked
        assert f"ton:pub:{pub}" in marked


# ── 402 mint wires the claim secret correctly ────────────────────────────

async def test_build_402_response_seeds_claim_secret_matching_wire_nonce(app_factory, tmp_path):
    """End-to-end sanity: the nonce minted by a real 402 response is itself
    claimable — split_full_nonce(payment_options[].nonce) matches a row the
    402 path inserted, with the right TTL."""
    app = app_factory()
    async with _serve_app(app) as c:
        resp = await c.post("/invoke", json={"capability": "translate"})
        assert resp.status == 402
        data = await resp.json()
        full_nonce = data["payment_options"][0]["nonce"]

        split = split_full_nonce(full_nonce)
        assert split is not None
        pub, pub_nonce, sec = split

        row = await app.claim_secrets.check(pub)
        assert row is not None
        secret_hash, expires_at = row
        assert secret_hash == hashlib.sha256(sec.encode()).hexdigest()
        # TTL is a fixed housekeeping window, independent of payment_timeout
        # (see _invoke_helpers.CLAIM_SECRET_TTL_SECONDS) — just assert it's
        # comfortably in the future, not tied to the settings value.
        assert expires_at > int(time.time()) + 60
