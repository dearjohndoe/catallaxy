"""Remote wallet monitor — thin HTTP client to tonapi-relay.

Used by `PaymentVerifier` / `JettonPaymentVerifier` when env var
`MONITOR_SERVICE_URL` is set. The relay receives TonAPI webhooks
and stores tx history; this client just looks up by nonce.

Interface mirrors `WalletMonitor` / `JettonWalletMonitor` so the
verifiers don't need rail-specific branches: `get`, `consume`, `force`,
`is_healthy`, `start`, `stop`, `replace_client` (no-op).
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, Optional

import aiohttp

from .types import JettonPaymentTx

logger = logging.getLogger(__name__)


# Health-check cache: don't hit /health on every is_healthy() call,
# verify() may call it inline. Refreshed lazily by an async helper.
_HEALTH_CHECK_INTERVAL = 30.0


class _RelayClient:
    """Shared aiohttp session + tonapi-relay endpoint URLs."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        s = self._session
        self._session = None
        if s is not None and not s.closed:
            await s.close()

    async def _reset_session(self) -> None:
        """Drop the current session so the next call reconnects.

        A session whose underlying keep-alive connection went stale is NOT
        reported as ``closed``, so ``_ensure`` would otherwise keep reusing a
        dead session forever — every request then times out and the remote
        monitor latches unhealthy with no way to self-heal. Resetting here
        forces ``_ensure`` to build a fresh session on the next call.
        """
        s = self._session
        self._session = None
        if s is not None and not s.closed:
            try:
                await s.close()
            except Exception:
                pass

    async def fetch_by_nonce(self, nonce: str, rail: str) -> Optional[dict[str, Any]]:
        s = await self._ensure()
        try:
            async with s.get(
                f"{self._base}/tx/by_nonce",
                params={"nonce": nonce, "rail": rail},
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    logger.warning("relay /tx/by_nonce HTTP %d: %s", resp.status, body)
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("relay /tx/by_nonce error: %s", e)
            await self._reset_session()
            return None

    async def subscribe(
        self,
        agent_wallet: Optional[str],
        jetton_wallet: Optional[str],
        label: Optional[str],
    ) -> dict[str, Any]:
        s = await self._ensure()
        body = {"agent_wallet": agent_wallet, "jetton_wallet": jetton_wallet, "label": label}
        async with s.post(f"{self._base}/subscribe", json=body) as resp:
            if resp.status >= 400:
                text = (await resp.text())[:200]
                raise RuntimeError(f"relay /subscribe HTTP {resp.status}: {text}")
            return await resp.json()

    async def health(self) -> Optional[dict[str, Any]]:
        s = await self._ensure()
        try:
            async with s.get(f"{self._base}/health") as resp:
                if resp.status >= 400:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("relay /health error: %s", e)
            await self._reset_session()
            return None


def _wrap_ton_tx(data: dict[str, Any]) -> SimpleNamespace:
    """Build a duck-typed Transaction matching what PaymentVerifier.verify() reads.

    Required fields: tx.now, tx.in_msg.info.src.to_str(), tx.in_msg.info.value.grams,
    tx.in_msg.body (used by _parse_payment_nonce — None is fine, parser returns ""),
    tx.cell.hash (bytes; verify calls `.hex()`).
    """
    sender = data.get("sender") or ""
    amount = int(data.get("amount") or 0)
    utime = int(data.get("utime") or 0)
    tx_hash_hex = data.get("tx_hash") or ""
    try:
        tx_hash_bytes = bytes.fromhex(tx_hash_hex)
    except ValueError:
        tx_hash_bytes = b""
    return SimpleNamespace(
        now=utime,
        in_msg=SimpleNamespace(
            info=SimpleNamespace(
                src=SimpleNamespace(
                    to_str=lambda *_, **__: sender,
                ),
                value=SimpleNamespace(grams=amount),
            ),
            body=None,
        ),
        cell=SimpleNamespace(hash=tx_hash_bytes),
    )


def _wrap_jetton_entry(data: dict[str, Any]) -> JettonPaymentTx:
    """Reconstruct a JettonPaymentTx from the relay's flat row."""
    utime = int(data.get("utime") or 0)
    tx_hash_hex = data.get("tx_hash") or ""
    try:
        tx_hash_bytes = bytes.fromhex(tx_hash_hex)
    except ValueError:
        tx_hash_bytes = b""
    tx_wrapper = SimpleNamespace(
        now=utime,
        cell=SimpleNamespace(hash=tx_hash_bytes),
    )
    return JettonPaymentTx(
        tx=tx_wrapper,  # type: ignore[arg-type]
        amount=int(data.get("amount") or 0),
        sender=str(data.get("sender") or ""),
        nonce=str(data.get("nonce") or ""),
    )


class _BaseRemoteMonitor:
    """Common scaffolding for TON and Jetton remote monitors."""
    RAIL: str = "TON"

    def __init__(
        self,
        relay: _RelayClient,
        account_id: str,
        label: Optional[str] = None,
    ) -> None:
        self._relay = relay
        self._account_id = account_id
        self._label = label
        # Local cache. Filled by get(); cleared by consume(). Replay protection
        # ultimately lives in `tx_store.is_processed` — clearing here is just
        # bookkeeping so a second verify on the same nonce doesn't see ghosts.
        self._by_nonce: dict[str, Any] = {}
        self._last_successful_poll_at: float = 0.0
        # Cached health (avoid hammering /health on every is_healthy call)
        self._health_cache: tuple[float, bool] = (0.0, True)

    async def start(self) -> None:
        # Subscription is performed at the verifier level so it can pass both
        # agent_wallet and jetton_wallet in a single /subscribe call.
        # Subclasses may override if they want to self-subscribe.
        pass

    async def stop(self) -> None:
        # Session is shared via _RelayClient; closed at SidecarApp shutdown.
        pass

    async def replace_client(self, client: Any) -> None:
        # No LiteBalancer to replace.
        return

    def force(self) -> None:
        # Relay polls TonAPI on its own schedule; we cannot push.
        return

    def is_healthy(self, max_age_seconds: float = 120.0) -> bool:
        """Best-effort. Returns cached True unless /health was recently checked
        and reported staleness beyond max_age_seconds. We deliberately allow
        verify() to attempt anyway — if relay is slow but not dead, the
        per-call 3x retry covers most cases.
        """
        cached_at, cached_ok = self._health_cache
        if time.time() - cached_at < _HEALTH_CHECK_INTERVAL:
            return cached_ok
        # Stale cache — schedule async refresh, return last known value.
        try:
            asyncio.get_running_loop().create_task(self._refresh_health(max_age_seconds))
        except RuntimeError:
            pass
        return cached_ok

    async def _refresh_health(self, max_age_seconds: float) -> None:
        """Remote-relay semantics for `is_healthy`:

        - Relay unreachable → unhealthy (can't verify anything).
        - Relay alive + at least one initial sync completed → healthy.
          Even if `last_webhook_at` is null and `last_sync_age_sec` is high
          (e.g. between 10-min sync cycles), the relay can still pick up the
          user's tx via webhook within seconds or via next sync ≤10 min.
        - Relay alive but sync never ran → unhealthy (no catch-up channel yet).

        The `max_age_seconds` param is kept for interface compatibility with
        local WalletMonitor but isn't applied here — remote means we trust
        the relay's own cadence, not absolute recency of a specific source.
        """
        info = await self._relay.health()
        if info is None:
            # The failed call above reset the session; retry once on a fresh
            # connection so a single stale keep-alive doesn't latch unhealthy.
            info = await self._relay.health()
        if info is None:
            self._health_cache = (time.time(), False)
            return
        last_sync_at = info.get("last_sync_at") or 0
        ok = bool(last_sync_at and last_sync_at > 0)
        self._health_cache = (time.time(), ok)

    async def get(self, nonce: str) -> Optional[Any]:
        """Single-shot lookup against the relay.

        No internal retry/sleep: the caller (`PaymentVerifier.verify`) already
        loops to its own deadline polling every VERIFY_POLL (~0.5s), so a tx
        gets picked up within ~0.5s of landing in the relay instead of being
        gated by a coarse multi-second internal retry. One retry layer, one
        place that owns the timeout.
        """
        nonce = nonce.strip()
        if nonce in self._by_nonce:
            return self._by_nonce[nonce]
        data = await self._relay.fetch_by_nonce(nonce, self.RAIL)
        if data is not None:
            self._last_successful_poll_at = time.time()
            wrapped = self._wrap(data)
            self._by_nonce[nonce] = wrapped
            return wrapped
        return None

    async def consume(self, nonce: str) -> Optional[Any]:
        return self._by_nonce.pop(nonce.strip(), None)

    def _wrap(self, data: dict[str, Any]) -> Any:
        raise NotImplementedError


class RemoteWalletMonitor(_BaseRemoteMonitor):
    """TON-rail remote monitor."""
    RAIL = "TON"

    def _wrap(self, data: dict[str, Any]) -> Any:
        return _wrap_ton_tx(data)


class RemoteJettonWalletMonitor(_BaseRemoteMonitor):
    """USDT-rail remote monitor."""
    RAIL = "USDT"

    def _wrap(self, data: dict[str, Any]) -> JettonPaymentTx:
        return _wrap_jetton_entry(data)


# Module-level helpers -------------------------------------------------------


def get_relay_url() -> Optional[str]:
    """Read MONITOR_SERVICE_URL env. Empty / unset means 'don't use remote'."""
    import os
    url = os.environ.get("MONITOR_SERVICE_URL", "").strip()
    return url or None
